# Removes Claude Pet: kills the running app, deletes the startup shortcut and install folder.

$installDir = "$env:LOCALAPPDATA\ClaudePet"
$startupFolder = [Environment]::GetFolderPath("Startup")
$shortcutPath = Join-Path $startupFolder "ClaudePet.lnk"

Get-Process pythonw -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and (Test-Path $installDir) -and ($_.Path -eq (Get-Command pythonw.exe -ErrorAction SilentlyContinue).Source) } |
    Stop-Process -Force -ErrorAction SilentlyContinue

if (Test-Path $shortcutPath) { Remove-Item $shortcutPath -Force }
if (Test-Path $installDir) { Remove-Item $installDir -Recurse -Force }

Write-Host "Claude Pet removed."
