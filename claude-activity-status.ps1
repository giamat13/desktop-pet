# ClaudePet activity bridge.
#
# Registered as a Claude Code hook (UserPromptSubmit / PreToolUse /
# PostToolUse / Stop - see install.ps1 for exact wiring) so the desktop pet
# can mirror what Claude is doing right now, the same way the terminal shows
# "puzzling..." or a tool name. Fires on every matching event in EVERY Claude
# Code session on this machine, not just one project.
#
# One file PER SESSION (activity-<session_id>.json), not one shared file: with
# two sessions open, a single file meant the busier one overwrote the other
# every few hundred ms and the pet showed a flickering mix of both. Per-session
# files let the pet animate the most recent session and still list the rest.
#
# Claude Code pipes one JSON object per invocation to stdin, always including
# hook_event_name, plus tool_name for the PreToolUse/PostToolUse events and
# session_id / cwd for all of them.

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

# Older Claude Code builds may not send session_id; one shared bucket then
# behaves exactly like the previous single-file version rather than breaking.
$sid = $data.session_id
if (-not $sid) { $sid = "default" }
$sid = $sid -replace '[^a-zA-Z0-9_-]', ''
if (-not $sid) { $sid = "default" }

$path = Join-Path $configDir "activity-$sid.json"
$tmp = "$path.tmp"

# The pet labels each session by its project folder, not by the raw id - a
# 36-char uuid tells you nothing about which window is busy.
$label = $null
if ($data.cwd) { $label = Split-Path -Leaf $data.cwd }

$obj = [ordered]@{
    state      = $state
    tool       = $data.tool_name
    label      = $label
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
