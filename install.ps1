# Desktop Pet installer
# Finds one consistent Python interpreter, installs pywebview into it,
# copies the app to %LOCALAPPDATA%\DesktopPet, registers it to run at
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

$installDir = "$env:LOCALAPPDATA\DesktopPet"

# Re-running this script updates an existing install. A pet already running
# holds the OLD code, so stop it first or the copied files won't take effect
# until the next login.
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
    Where-Object { $_.CommandLine -like "*DesktopPet*pet_app.py*" } |
    ForEach-Object { Write-Host "Stopping running Desktop Pet (pid $($_.ProcessId))..."; Stop-Process -Id $_.ProcessId -Force }

New-Item -ItemType Directory -Force -Path $installDir | Out-Null
Copy-Item -Path "$PSScriptRoot\pets" -Destination $installDir -Recurse -Force
Copy-Item -Path "$PSScriptRoot\pet_app.py" -Destination $installDir -Force
Copy-Item -Path "$PSScriptRoot\claude-usage-statusline.ps1" -Destination $installDir -Force
Copy-Item -Path "$PSScriptRoot\refresh_usage.py" -Destination $installDir -Force
Copy-Item -Path "$PSScriptRoot\claude-activity-status.ps1" -Destination $installDir -Force

# Give the VLC pet something to read a playback position from.
#
# VLC 3.x does not publish to Windows' media sessions (SMTC) at all - there is
# no such plugin in its plugins\ tree - so the route every other app's progress
# comes from is simply unavailable for it. Its own HTTP interface is the
# supported alternative, but it is off by default and has to be enabled in
# vlcrc. Bound to 127.0.0.1 and password-protected, so nothing is exposed off
# this machine. VLC only reads vlcrc at startup: an already-running VLC has to
# be restarted before the pet's bar appears.
$vlcrc = "$env:APPDATA\vlc\vlcrc"
if (Test-Path $vlcrc) {
    $desired = @{
        'extraintf'     = 'http'
        'http-host'     = '127.0.0.1'
        'http-port'     = '8080'
        'http-password' = 'desktoppet'   # must match VLC_HTTP_PASSWORD in pet_app.py
    }
    $lines = Get-Content $vlcrc
    foreach ($key in $desired.Keys) {
        $value = $desired[$key]
        # The setting exists in vlcrc already, commented out with its default.
        # Rewrite that line in place so VLC keeps it in the section it expects,
        # and only append if this build genuinely has no such key.
        $pattern = "^#?$([regex]::Escape($key))=.*$"
        if ($lines -match $pattern) {
            $lines = $lines -replace $pattern, "$key=$value"
        } else {
            $lines += "$key=$value"
        }
    }
    Set-Content -Path $vlcrc -Value $lines -Encoding UTF8
    Write-Host "Enabled VLC's local HTTP interface (restart VLC for the pet's progress bar)."
} else {
    Write-Host "VLC config not found - skipping VLC progress-bar setup (run VLC once, then re-run this script)."
}

# Keep the usage gauges fed.
#
# Claude Code only renders its status line - and therefore only runs
# claude-usage-statusline.ps1 - inside an interactive terminal session. It
# never fires in the VS Code extension, so without this the gauges would only
# ever update on the rare occasions the user ran `claude` in a real terminal.
# refresh_usage.py drives one short session in a hidden ConPTY to collect the
# numbers. Each run costs one tiny API turn, so keep the interval modest.
#
# To stop it:    Unregister-ScheduledTask -TaskName DesktopPetUsageRefresh -Confirm:$false
$taskName = "DesktopPetUsageRefresh"
$action = New-ScheduledTaskAction -Execute $pythonwExe `
    -Argument "`"$installDir\refresh_usage.py`"" -WorkingDirectory $installDir
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 3) `
    -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -Description "Refreshes DesktopPet usage gauges" -Force | Out-Null
Write-Host "Registered usage refresh task '$taskName' (every 5 min)."

$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "DesktopPet.lnk"

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