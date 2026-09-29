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
#      (only on this PC, never uploaded anywhere; re-asked if the address is not @mcgill.ca)
#   5. starts the server once over stdio, like Claude Desktop does (self-test)
#   6. registers it in Claude Desktop's claude_desktop_config.json - Microsoft Store
#      (MSIX) builds included (a timestamped .bak of the old config is kept next to it)
#   7. restarts Claude Desktop so it loads the server
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
    $existing = @{}
    if (Test-Path $envFile) {
        foreach ($l in [IO.File]::ReadAllLines($envFile)) {
            if ($l -match '^\s*([A-Za-z_]+)\s*=\s*(.*)$') { $existing[$matches[1]] = $matches[2] }
        }
    }
    # McGill's SSO only accepts a McGill account: a Gmail/Outlook address ends in
    # AADSTS50020 ("account does not exist in tenant 'McGill University'").
    $userOk = [string]$existing['MCGILL_USERNAME'] -match 'mcgill\.ca'
    if ((Test-Path $envFile) -and $userOk) {
        Ok ".env already exists with a McGill address - kept as is"
    } else {
        if (Test-Path $envFile) { Warn "The username in .env is not a McGill address - asking again." }
        function Quote-Env([string]$s) { "'" + (($s -replace '\\', '\\') -replace "'", "\'") + "'" }
        do {
            $user = (Read-Host "McGill email (firstname.lastname@mail.mcgill.ca - NOT your Gmail)").Trim()
            if ($user -notmatch '@(mail\.)?mcgill\.ca$') { Warn "That is not a McGill address (must end with @mail.mcgill.ca or @mcgill.ca)." }
        } until ($user -match '@(mail\.)?mcgill\.ca$')
        $sec  = Read-Host "McGill password (the one you use for myCourses/Minerva; stored only in $envFile)" -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
        try { $pw = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
        finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
        $icalLine = $null
        $oldIcal = [string]$existing['MCGILL_ICAL_URL']
        if ($oldIcal -match 'https?://') {
            $icalLine = "MCGILL_ICAL_URL=$oldIcal"
            Ok "Keeping the calendar feed URL already in .env"
        } else {
            Write-Host "    myCourses calendar feed: myCourses > Calendar > Subscribe > 'All Calendars and Tasks' > copy the URL."
            Write-Host "    It is a password-like secret: never share it."
            $ical = Read-Host "Calendar feed URL (Enter to skip)"
            $icalLine = "MCGILL_ICAL_URL=$(Quote-Env $ical.Trim())"
        }
        $lines = @(
            "MCGILL_USERNAME=$(Quote-Env $user)",
            "MCGILL_PASSWORD=$(Quote-Env $pw)",
            "HEADLESS=True",
            "TIMEOUT=30000",
            "SESSION_STATE_PATH=.mcgill_session.json",
            "MYCOURSES_TIMEZONE=America/Toronto",
            $icalLine
        )
        [IO.File]::WriteAllText($envFile, (($lines -join "`r`n") + "`r`n"), $Utf8NoBom)
        $pw = $null
        # a session cached for another account (or a failed login) must not be reused
        Remove-Item -Force -ErrorAction SilentlyContinue (Join-Path $Dest '.mcgill_session.json')
        Ok ".env written"
    }

    # ---------------------------------------------------------- 5. self-test
    Step "Self-test: starting the server the way Claude Desktop does"
    & $venvPy (Join-Path $Dest 'testing\selftest_stdio.py')
    if ($LASTEXITCODE -ne 0) { throw "The MCP server does not start correctly - see the error above." }

    # ------------------------------------------------------ 6. Claude Desktop
    Step "Registering the server in Claude Desktop"
    # Microsoft Store / MSIX builds of Claude Desktop read their config from a
    # private package folder, NOT from %APPDATA%\Claude. Classic installs use
    # %APPDATA%\Claude. Write to the right one (create it if needed).
    $configDirs = @()
    $pkgRoot = Join-Path $env:LOCALAPPDATA 'Packages'
    $msix = $null
    if (Test-Path $pkgRoot) {
        $msix = Get-ChildItem -Path $pkgRoot -Filter 'Claude_*' -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
    }
    if ($msix) {
        Ok "Claude Desktop (Microsoft Store / MSIX version) detected: $($msix.Name)"
        $configDirs += (Join-Path $msix.FullName 'LocalCache\Roaming\Claude')
        $appdataDir = Join-Path $env:APPDATA 'Claude'
        if (Test-Path (Join-Path $appdataDir 'claude_desktop_config.json')) { $configDirs += $appdataDir }
    } else {
        $configDirs += (Join-Path $env:APPDATA 'Claude')
    }

    $entry = [pscustomobject]@{ command = $venvPy; args = @((Join-Path $Dest 'mcp_server.py')) }
    foreach ($dir in $configDirs) {
        New-Item -ItemType Directory -Force -Path $dir | Out-Null
        $cfg = Join-Path $dir 'claude_desktop_config.json'
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
        Ok "Registered 'mycourses-mcgill' in $cfg"
    }

    # ------------------------------------------------------- 7. restart Claude
    Step "Restarting Claude Desktop (closing the window is not enough: it keeps running in the tray)"
    $procs = @(Get-Process -Name 'claude' -ErrorAction SilentlyContinue)
    if ($procs.Count -gt 0) {
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 3
    }
    $launched = $false
    try {
        if ($msix -and (Get-Command Get-AppxPackage -ErrorAction SilentlyContinue)) {
            $appx = Get-AppxPackage | Where-Object { $_.PackageFamilyName -eq $msix.Name } | Select-Object -First 1
            if ($appx) {
                $appId = @((Get-AppxPackageManifest $appx).Package.Applications.Application)[0].Id
                Start-Process "shell:AppsFolder\$($appx.PackageFamilyName)!$appId"
                $launched = $true
            }
        } else {
            $exe = Join-Path $env:LOCALAPPDATA 'AnthropicClaude\claude.exe'
            if (Test-Path $exe) { Start-Process $exe; $launched = $true }
        }
    } catch { }
    if ($launched) { Ok "Claude Desktop restarted" } else { Warn "Reopen Claude Desktop from the Start menu." }

    Step "Done"
    Write-Host "  In a new Claude chat, ask: 'list my myCourses courses'. The first time, a browser window"
    Write-Host "  opens: sign in with your McGill account and type your Authenticator code there."
    Write-Host "  After that the session is cached and invisible."
    Write-Host "  To update later, or to re-enter credentials: run the same one-line command again"
    Write-Host "  (delete $envFile first to change them)."
}
