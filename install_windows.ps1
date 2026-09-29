# install_windows.ps1 - one-shot installer for the McGill myCourses MCP server on Windows.
#
# Usage (in a normal PowerShell window, no admin needed):
#   irm https://raw.githubusercontent.com/coucou330/mcgill-mycourses-mcp/main/install_windows.ps1 | iex
#
# What it does:
#   1. downloads this repo to %USERPROFILE%\mcgill-mycourses-mcp (re-running = update;
#      your .env, cached session and venv are kept)
#   2. finds Python 3.12/3.11 (installs 3.12 with winget if missing)
#   3. creates the venv, installs the requirements and Playwright's Chromium
#   4. asks for your McGill email/password + calendar feed URL and writes .env
#      (only on this PC, never uploaded anywhere)
#   5. registers the server in Claude Desktop's claude_desktop_config.json
#      (a timestamped .bak copy of the old config is kept next to it)
#
# Set $env:MYCOURSES_REPO = "owner/repo" before running to install from another fork.

& {
    $ErrorActionPreference = 'Stop'
    $ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is very slow on PS 5.1 otherwise

    $Repo   = if ($env:MYCOURSES_REPO) { $env:MYCOURSES_REPO } else { 'coucou330/mcgill-mycourses-mcp' }
    $Branch = 'main'
    $Dest   = Join-Path $env:USERPROFILE 'mcgill-mycourses-mcp'
    $Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

    function Step($m) { Write-Host ""; Write-Host "==> $m" -ForegroundColor Cyan }
    function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
    function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
    function Run($exe, [string[]]$argList) {
        & $exe @argList
        if ($LASTEXITCODE -ne 0) { throw "Command failed ($LASTEXITCODE): $exe $($argList -join ' ')" }
    }

    # ---------------------------------------------------------------- 1. code
    Step "Downloading $Repo ($Branch) to $Dest"
    $tmp = Join-Path $env:TEMP ("mycourses-mcp-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp | Out-Null
    $zip = Join-Path $tmp 'src.zip'
    Invoke-WebRequest -UseBasicParsing -Uri "https://github.com/$Repo/archive/refs/heads/$Branch.zip" -OutFile $zip
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $src = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Copy-Item -Path (Join-Path $src.FullName '*') -Destination $Dest -Recurse -Force
    Remove-Item -Recurse -Force $tmp
    Ok "Code is in $Dest"

    # -------------------------------------------------------------- 2. python
    Step "Looking for Python 3.12 / 3.11"
    function Find-Python {
        $candidates = @(
            @{ exe = 'py'; pre = @('-3.12') },
            @{ exe = 'py'; pre = @('-3.11') },
            @{ exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'); pre = @() },
            @{ exe = (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'); pre = @() },
            @{ exe = 'python'; pre = @() }
        )
        foreach ($c in $candidates) {
            try {
                $pre = [string[]]$c.pre
                $v = & $c.exe @pre -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
                if ($LASTEXITCODE -eq 0 -and ($v -eq '3.12' -or $v -eq '3.11')) { return $c }
            } catch { }
        }
        return $null
    }
    $py = Find-Python
    if (-not $py) {
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Warn "Python 3.12 not found - installing it with winget (user scope, 1-2 min)..."
            & winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
            $py = Find-Python
        }
        if (-not $py) {
            throw "Python 3.12 is required. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run this script again."
        }
    }
    Ok "Using: $($py.exe) $($py.pre -join ' ')"

    # ---------------------------------------------------------- 3. venv + deps
    Step "Creating the virtual environment and installing dependencies (a few minutes)"
    $venvPy = Join-Path $Dest 'venv\Scripts\python.exe'
    if (-not (Test-Path $venvPy)) {
        Run $py.exe ([string[]]$py.pre + @('-m', 'venv', (Join-Path $Dest 'venv')))
    }
    Run $venvPy @('-m', 'pip', 'install', '--quiet', '--upgrade', 'pip')
    Run $venvPy @('-m', 'pip', 'install', '--quiet', '-r', (Join-Path $Dest 'requirements.txt'))
    Step "Installing Playwright's Chromium"
    Run $venvPy @('-m', 'playwright', 'install', 'chromium')
    Ok "Dependencies installed"

    # ---------------------------------------------------------------- 4. .env
    Step "Credentials (.env)"
    $envFile = Join-Path $Dest '.env'
    if (Test-Path $envFile) {
        Ok ".env already exists - kept as is (edit it with Notepad to change anything)"
    } else {
        function Quote-Env([string]$s) { "'" + (($s -replace '\\', '\\') -replace "'", "\'") + "'" }
        $user = Read-Host "McGill email (e.g. firstname.lastname@mail.mcgill.ca)"
        $sec  = Read-Host "McGill password (stored only in $envFile on this PC)" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $pw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        Write-Host "    myCourses calendar feed: myCourses > Calendar > Subscribe > 'All Calendars and Tasks' > copy the URL."
        Write-Host "    It is a password-like secret: never share it."
        $ical = Read-Host "Calendar feed URL (Enter to skip)"
        $lines = @(
            "MCGILL_USERNAME=$(Quote-Env $user)",
            "MCGILL_PASSWORD=$(Quote-Env $pw)",
            "HEADLESS=True",
            "TIMEOUT=30000",
            "SESSION_STATE_PATH=.mcgill_session.json",
            "MYCOURSES_TIMEZONE=America/Toronto",
            "MCGILL_ICAL_URL=$(Quote-Env $ical.Trim())"
        )
        [IO.File]::WriteAllText($envFile, (($lines -join "`r`n") + "`r`n"), $Utf8NoBom)
        $pw = $null
        Ok ".env written"
    }

    # ---------------------------------------------------------- 5. self-test
    Step "Self-test"
    Push-Location $Dest
    try {
        Run $venvPy @('-c', 'import mcp_server; print(''    MCP server imports OK'')')
        $calTest = 'import os; from dotenv import load_dotenv; load_dotenv(''.env'', interpolate=False); from brightspace_api import fetch_calendar_events, filter_events_by_range; u = os.getenv(''MCGILL_ICAL_URL''); print(''    Calendar feed: '' + (str(len(filter_events_by_range(fetch_calendar_events(u), 14, 0))) + '' events in the next 14 days'' if u else ''skipped (no URL in .env)''))'
        & $venvPy -c $calTest
        if ($LASTEXITCODE -ne 0) { Warn "Calendar feed test failed - check MCGILL_ICAL_URL in .env" }
    } finally { Pop-Location }

    # ------------------------------------------------------ 6. Claude Desktop
    Step "Registering the server in Claude Desktop"
    $candidates = @(Join-Path $env:APPDATA 'Claude\claude_desktop_config.json')
    $pkgRoot = Join-Path $env:LOCALAPPDATA 'Packages'
    if (Test-Path $pkgRoot) {
        $candidates += @(Get-ChildItem -Path $pkgRoot -Filter 'Claude_*' -Directory -ErrorAction SilentlyContinue |
            ForEach-Object { Join-Path $_.FullName 'LocalCache\Roaming\Claude\claude_desktop_config.json' })
    }
    $targets = @($candidates | Where-Object { Test-Path $_ })
    if ($targets.Count -eq 0 -and (Test-Path (Join-Path $env:APPDATA 'Claude'))) { $targets = @($candidates[0]) }

    $entry = [pscustomobject]@{ command = $venvPy; args = @((Join-Path $Dest 'mcp_server.py')) }
    if ($targets.Count -eq 0) {
        Warn "Claude Desktop config not found. In Claude Desktop: Settings > Developer > Edit Config, and add under mcpServers:"
        Write-Host ((@{ 'mycourses-mcgill' = $entry } | ConvertTo-Json -Depth 5))
    }
    foreach ($cfg in $targets) {
        $json = [pscustomobject]@{}
        if (Test-Path $cfg) {
            Copy-Item $cfg ("$cfg.bak-" + (Get-Date -Format 'yyyyMMdd-HHmmss'))
            $raw = [IO.File]::ReadAllText($cfg)
            if ($raw.Trim()) { $json = $raw | ConvertFrom-Json }
        }
        if (-not ($json.PSObject.Properties.Name -contains 'mcpServers')) {
            $json | Add-Member -NotePropertyName 'mcpServers' -NotePropertyValue ([pscustomobject]@{})
        }
        $json.mcpServers | Add-Member -NotePropertyName 'mycourses-mcgill' -NotePropertyValue $entry -Force
        [IO.File]::WriteAllText($cfg, ($json | ConvertTo-Json -Depth 32), $Utf8NoBom)
        Ok "Added 'mycourses-mcgill' to $cfg (backup kept next to it)"
    }

    Step "Done"
    Write-Host "  1. Quit Claude Desktop completely (tray icon near the clock > Quit), then reopen it."
    Write-Host "  2. In a chat, ask: 'list my myCourses courses'. The first time, a browser window opens:"
    Write-Host "     type your Authenticator code there. After that the session is cached and invisible."
    Write-Host "  To update later: run the same one-line command again."
}
