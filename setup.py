"""
Setup script for Brightspace MCP Server
"""

import subprocess
import sys
import os


def create_virtual_environment():

    """Create virtual environment for the project"""
    print("Creating virtual environment...")
    
    # Try Python 3.12 first (more compatible with Playwright). On Windows the
    # python.org installer provides the `py` launcher (and `python`), not
    # `python3.12` - and `python3` there is often the Microsoft Store stub.
    candidates = [
        ["py", "-3.12"], ["python3.12"],
        ["py", "-3.11"], ["python3.11"],
        ["python3"], ["python"],
    ]
    python_executable = None

    for cmd in candidates:
        try:
            result = subprocess.run(
                cmd + ["-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            continue
        version = result.stdout.strip()
        if result.returncode == 0 and version in ("3.11", "3.12"):
            print(f"Using {' '.join(cmd)}: Python {version}")
            python_executable = cmd
            break

    if not python_executable:
        print("Error: No compatible Python version found. Please install Python 3.12 "
              "(Windows: `winget install -e --id Python.Python.3.12`)")
        sys.exit(1)

    subprocess.check_call(python_executable + ["-m", "venv", "venv"])
    print("✓ Virtual environment created")
    
    # Determine the correct activation script path and Python executable
    if os.name == 'nt':  # Windows
        activate_script = os.path.join("venv", "Scripts", "activate.bat")
        venv_python_executable = os.path.join("venv", "Scripts", "python.exe")
    else:  # Unix/Linux/macOS
        activate_script = os.path.join("venv", "bin", "activate")
        venv_python_executable = os.path.join("venv", "bin", "python")
    
    print(f"✓ Virtual environment created at: {os.path.abspath('venv')}")
    print(f"✓ To activate: source {activate_script}" if os.name != 'nt' else f"✓ To activate: {activate_script}")
    
    return venv_python_executable


def install_requirements(python_executable=None):
    """Install required Python packages"""
    if python_executable is None:
        python_executable = sys.executable
        
    print("Installing Python requirements...")
    subprocess.check_call([python_executable, "-m", "pip", "install", "--upgrade", "pip"])
    subprocess.check_call([python_executable, "-m", "pip", "install", "-r", "requirements.txt"])
    print("✓ Python requirements installed")


def install_playwright_browsers(python_executable=None):
    """Install Playwright browsers"""
    if python_executable is None:
        python_executable = sys.executable
        
    print("Installing Playwright browsers...")
    subprocess.check_call([python_executable, "-m", "playwright", "install", "chromium"])
    print("✓ Playwright browsers installed")


def create_env_file():
    """Create .env file template"""
    env_content = """# McGill myCourses Credentials
MCGILL_USERNAME=your_mcgill_username_or_email
MCGILL_PASSWORD=your_mcgill_password

# Scraping Configuration
# Leave this True and forget about it: login() auto-detects when a real
# login is actually needed (no cached session yet, or an expired one) and
# transparently pops up a visible window just for that - McGill's MFA is a
# TOTP code you type into the page yourself, so it can't be done headless.
# Every other call stays invisible, restoring the cached session
# (SESSION_STATE_PATH below) instead of hitting the login form at all.
HEADLESS=True
TIMEOUT=30000

# Where the authenticated session (cookies) is cached between runs, so the
# MCP server doesn't have to log in - and enter a new MFA code - every single
# tool call.
SESSION_STATE_PATH=.mcgill_session.json

# Optional but recommended: powers get_calendar, which is more reliable than
# get_assignments (no login/MFA/browser needed at all, covers every course in
# one call). Get this from myCourses: Calendar > Subscribe > "All Calendars
# and Tasks" > copy the feed URL. TREAT THIS LIKE A PASSWORD - the token in
# the URL grants read access to your whole calendar to anyone who has it. If
# it's ever exposed, reset it from that same Subscribe dialog.
MCGILL_ICAL_URL=
"""
    
    if not os.path.exists(".env"):
        with open(".env", "w") as f:
            f.write(env_content)
        print("✓ Created .env file template")
    else:
        print("✓ .env file already exists")


def main():
    """Run setup"""
    print("Setting up Brightspace MCP Server...")
    
    try:
        # Create virtual environment
        python_executable = create_virtual_environment()
        
        # Install requirements in the virtual environment
        install_requirements(python_executable)
        install_playwright_browsers(python_executable)
        create_env_file()
        
        print("\n🎉 Setup complete!")
        print("\nNext steps:")
        print("1. Activate the virtual environment:")
        if os.name == 'nt':  # Windows
            print("   venv\\Scripts\\activate")
        else:  # Unix/Linux/macOS
            print("   source venv/bin/activate")
        print("2. Edit .env file with your McGill credentials")
        print("3. Run: python testing/playwright_trial.py")
        print("4. Run: python brightspace_api.py")
        
    except Exception as e:
        print(f"Setup failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
