# Desktop Pet installer
# Finds one consistent Python interpreter, installs pywebview into it,
# copies the app to %LOCALAPPDATA%\ClaudePet, registers it to run at
# every login, and starts it immediately.

$ErrorActionPreference = "Stop"

function Get-PythonExe {
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $exe = & py -3 -c "import sys; print(sys.executable)"
        if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
    }
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) { return $python.Source }
    throw "No Python installation found. Install Python from https://python.org and re-run this script."
}

$pythonExe = Get-PythonExe
Write-Host "Using Python: $pythonExe"

$pythonDir = Split-Path $pythonExe -Parent
$pythonwExe = Join-Path $pythonDir "pythonw.exe"
if (-not (Test-Path $pythonwExe)) { $pythonwExe = $pythonExe }

Write-Host "Installing dependencies (pywebview + pywin32)..."
& $pythonExe -m pip install --quiet --upgrade -r "$PSScriptRoot\requirements.txt"

$installDir = "$env:LOCALAPPDATA\ClaudePet"

# Re-running this script updates an existing install. A pet already running
# holds the OLD code, so stop it first or the copied files won't take effect
# until the next login.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*ClaudePet*pet_app.py*" } |
    ForEach-Object { Write-Host "Stopping running Desktop Pet (pid $($_.ProcessId))..."; Stop-Process -Id $_.ProcessId -Force }

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -Path "$PSScriptRoot\claude_pet.html" -Destination $installDir -Force
Copy-Item -Path "$PSScriptRoot\vlc-pet.html" -Destination $installDir -Force
Copy-Item -Path "$PSScriptRoot\settings.html" -Destination $installDir -Force
Copy-Item -Path "$PSScriptRoot\pet_app.py" -Destination $installDir -Force

$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "ClaudePet.lnk"

$wsh = New-Object -ComObject WScript.Shell
$shortcut = $wsh.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonwExe
$shortcut.Arguments = "`"$installDir\pet_app.py`""
$shortcut.WorkingDirectory = $installDir
$shortcut.Save()

Write-Host "Installed to $installDir"
Write-Host "Will auto-start at login via: $shortcutPath"
Write-Host "Starting Desktop Pet now..."
Start-Process -FilePath $pythonwExe -ArgumentList "`"$installDir\pet_app.py`""