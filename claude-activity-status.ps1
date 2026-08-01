# ClaudePet activity bridge.
#
# Registered as a Claude Code hook (UserPromptSubmit / PreToolUse /
# PostToolUse / Stop - see install.ps1 for exact wiring) so the desktop pet
# can mirror what Claude is doing right now, the same way the terminal shows
# "puzzling..." or a tool name. Fires on every matching event in EVERY Claude
# Code session on this machine, not just one project - with two sessions
# open at once the pet just shows whichever fired most recently.
#
# Claude Code pipes one JSON object per invocation to stdin, always including
# hook_event_name, and tool_name for the PreToolUse/PostToolUse events.

$ErrorActionPreference = "SilentlyContinue"

$raw = [Console]::In.ReadToEnd()
if (-not $raw) { exit 0 }

try {
    $data = $raw | ConvertFrom-Json
} catch {
    exit 0
}

$state = switch ($data.hook_event_name) {
    "UserPromptSubmit" { "thinking" }
    "PreToolUse"        { "tool" }
    "PostToolUse"        { "thinking" }  # back to reasoning until the next tool or Stop
    "Stop"                { "idle" }
    default                { $null }
}
if (-not $state) { exit 0 }

$configDir = "$env:LOCALAPPDATA\DesktopPet"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$path = Join-Path $configDir "activity.json"
$tmp = "$path.tmp"

$obj = [ordered]@{
    state      = $state
    tool       = $data.tool_name
    updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

try {
    # Write-then-move so the pet never reads a half-written file.
    $obj | ConvertTo-Json -Compress | Set-Content -Path $tmp -Encoding utf8 -NoNewline
    Move-Item -Path $tmp -Destination $path -Force
} catch {
    # A missed write just means the pet's activity badge is briefly stale -
    # never worth surfacing as an error.
}
