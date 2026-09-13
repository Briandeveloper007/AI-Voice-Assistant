param(
    [bool]$InstallDeps = $true
)

if ($InstallDeps) {
    Write-Host "Checking for PyInstaller..."
    if (-not (Get-Command pyinstaller -ErrorAction SilentlyContinue)) {
        Write-Host "Installing PyInstaller..."
        pip install pyinstaller
    }
}

Write-Host "Building HarleyAssistant Standalone Executable..."

# Run pyinstaller with the required flags
pyinstaller --name "HarleyAssistant" --noconsole --paths . --add-data "models/hey_harley.onnx;models/" app/main.py

if ($?) {
    Write-Host "Build complete! Executable is located in the dist\ directory." -ForegroundColor Green
} else {
    Write-Host "Build failed." -ForegroundColor Red
}
