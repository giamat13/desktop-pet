# ClaudePet usage bridge.
#
# This is registered as Claude Code's `statusLine` command (see install.ps1).
# Claude Code invokes it on every status update and pipes session JSON to
# stdin, including `rate_limits.five_hour` / `rate_limits.seven_day` -
# Anthropic's own 5-hour and weekly usage percentages, the same numbers
# `/usage` shows. This script:
#
#   1. Writes those numbers to ClaudePet's config folder as usage.json, so
#      the desktop pet (pet_app.py) can read them and render the live/weekly
#      gauges - no scraping, no guessed limits, just what Claude Code itself
#      reports.
#   2. Still prints a normal status line to the terminal, so installing the
#      pet doesn't take away whatever the status line already showed.
#
# `rate_limits` is only present for Claude.ai subscribers (Pro/Max), and only
# after the first API response in a session - both fields are handled as
# optional throughout.

$ErrorActionPreference = "SilentlyContinue"

$raw = [Console]::In.ReadToEnd()
if (-not $raw) { exit 0 }

try {
    $data = $raw | ConvertFrom-Json
} catch {
    exit 0
}

$model        = $data.model.display_name
$ctxPct       = $data.context_window.used_percentage
$fiveHPct     = $data.rate_limits.five_hour.used_percentage
$fiveHResets  = $data.rate_limits.five_hour.resets_at
$sevenDPct    = $data.rate_limits.seven_day.used_percentage
$sevenDResets = $data.rate_limits.seven_day.resets_at

# --- write usage.json for the desktop pet ---
# Same folder pet_app.py already uses for config.json/log.txt (CONFIG_DIR).
$configDir = "$env:LOCALAPPDATA\ClaudePet"
New-Item -ItemType Directory -Force -Path $configDir | Out-Null
$usagePath = Join-Path $configDir "usage.json"
$tmpPath = "$usagePath.tmp"

$usage = [ordered]@{
    five_hour  = [ordered]@{ used_percentage = $fiveHPct; resets_at = $fiveHResets }
    seven_day  = [ordered]@{ used_percentage = $sevenDPct; resets_at = $sevenDResets }
    # Lets the pet tell "no Claude Code session running right now" apart from
    # "genuinely 0% used" - see read_usage()'s staleness check in pet_app.py.
    updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}

try {
    # Write-then-move so the pet never reads a half-written file.
    $usage | ConvertTo-Json -Compress -Depth 5 | Set-Content -Path $tmpPath -Encoding utf8 -NoNewline
    Move-Item -Path $tmpPath -Destination $usagePath -Force
} catch {
    # A missed write just means the pet's gauges show slightly stale data
    # until the next status line update - never worth surfacing as an error.
}

# --- normal terminal status line ---
$parts = @()
if ($model)          { $parts += "[$model]" }
if ($null -ne $ctxPct)   { $parts += "ctx $([math]::Round($ctxPct))%" }
if ($null -ne $fiveHPct) { $parts += "5h $([math]::Round($fiveHPct))%" }
if ($null -ne $sevenDPct){ $parts += "7d $([math]::Round($sevenDPct))%" }
Write-Host ($parts -join " | ")
