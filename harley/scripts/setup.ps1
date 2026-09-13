# setup.ps1 - Harley Voice Assistant Environment Setup
# Run from the harley\ root directory

Write-Host "=== Harley Voice Assistant Setup ===" -ForegroundColor Cyan

# -------------------------------------------------------------------
# 1. Create project sub-directories
# -------------------------------------------------------------------
$dirs = @(
    "src",
    "src\orchestrator",
    "src\audio",
    "src\tts",
    "src\executors",
    "src\ui",
    "src\native_host",
    "tests",
    "logs",
    "data"
)

foreach ($dir in $dirs) {
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir | Out-Null
        Write-Host "  Created: $dir" -ForegroundColor Green
    } else {
        Write-Host "  Exists:  $dir" -ForegroundColor Gray
    }
}

# -------------------------------------------------------------------
# 2. Create virtual environment
# -------------------------------------------------------------------
if (-not (Test-Path ".venv")) {
    Write-Host "`nCreating virtual environment (.venv)..." -ForegroundColor Cyan
    python -m venv .venv
    Write-Host "  Done." -ForegroundColor Green
} else {
    Write-Host "`nVirtual environment already exists, skipping." -ForegroundColor Gray
}

# -------------------------------------------------------------------
# 3. Upgrade pip inside the venv
# -------------------------------------------------------------------
Write-Host "`nUpgrading pip..." -ForegroundColor Cyan
& ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet

# -------------------------------------------------------------------
# 4. Install core dependencies
# -------------------------------------------------------------------
Write-Host "`nInstalling core dependencies..." -ForegroundColor Cyan

$packages = @(
    # Async & concurrency
    "asyncio-throttle",
    # AI / LLM
    "openai",
    # Audio capture (via sounddevice / PyAudio)
    "sounddevice",
    "pyaudio",
    # Speech recognition
    "faster-whisper",
    # TTS
    "pyttsx3",
    # Windows notifications (toasts)
    "windows-toasts",
    # Database
    "aiosqlite",
    # Schema validation
    "pydantic>=2.0",
    # Logging
    "loguru",
    # Testing
    "pytest",
    "pytest-asyncio"
)

foreach ($pkg in $packages) {
    Write-Host "  Installing $pkg ..." -ForegroundColor Yellow
    & ".venv\Scripts\pip.exe" install $pkg --quiet
}

# -------------------------------------------------------------------
# 5. Create placeholder __init__.py files
# -------------------------------------------------------------------
$initFiles = @(
    "src\__init__.py",
    "src\orchestrator\__init__.py",
    "src\audio\__init__.py",
    "src\tts\__init__.py",
    "src\executors\__init__.py",
    "src\ui\__init__.py",
    "src\native_host\__init__.py",
    "tests\__init__.py"
)

foreach ($f in $initFiles) {
    if (-not (Test-Path $f)) {
        New-Item -ItemType File -Path $f | Out-Null
    }
}

Write-Host "`n=== Setup complete! ===" -ForegroundColor Cyan
Write-Host "Activate with:  .\.venv\Scripts\Activate.ps1" -ForegroundColor White
