# migrate-hex.ps1 — bulk hex → CSS-token migration.
#
# Replaces the most common chrome (background / text / border) hex literals
# with `var(--token)` references. Semantic colors (bull/bear/caution/accent)
# are deliberately NOT migrated by default — they look identical across
# light + dark and the dim/border variants are token-aliased per-theme.
#
# Mapping derived from a frequency survey across the worst-offender files
# (app.js, market-visuals.js, index.html, curated-signals.js, phase_1_5.js,
# stock-popup.js) on 2026-05-17.
#
# Usage:
#   pwsh tools/migrate-hex.ps1 -Files app/shared/js/app.js,app/shared/js/market-visuals.js
#   pwsh tools/migrate-hex.ps1 -DryRun -Files <files>     # preview only
#
# Run check-no-hex.ps1 BEFORE and AFTER to measure violation reduction.

[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [string[]]$Files,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

# Ordered list — longest patterns first (8-digit alpha hexes are not in the
# safe list because alpha pairs need theme-aware tokenisation, which we
# can't do via pure find-replace). Replacements are case-insensitive on the
# hex digits but the var() output is canonical lowercase.
#
# Chrome-only colors: the page background, panel surfaces, body text,
# muted text, hairline borders. These are exactly where light/dark needs
# to diverge — replacing them with tokens fixes the "black spots".
$MAP = [ordered]@{
    # Page + panel surfaces
    '#080c12' = 'var(--surface-0)'    # legacy --bg
    '#0a0d13' = 'var(--surface-0)'
    '#0a0e14' = 'var(--surface-0)'
    '#10141a' = 'var(--surface-0)'
    '#0d1118' = 'var(--surface-1)'
    '#11151d' = 'var(--surface-1)'
    '#111720' = 'var(--surface-1)'
    '#161d28' = 'var(--surface-2)'
    '#181d28' = 'var(--surface-2)'
    '#1a1f2e' = 'var(--surface-2)'
    '#1c2435' = 'var(--surface-3)'
    '#222937' = 'var(--surface-3)'
    '#222c3d' = 'var(--surface-3)'
    '#2a3142' = 'var(--surface-3)'

    # Text hierarchy (dark-mode native + a few light-mode variants)
    '#e6eaf2' = 'var(--text-primary)'
    '#dde3ef' = 'var(--text-primary)'
    '#dfe2eb' = 'var(--text-primary)'
    '#f4f6fb' = 'var(--text-primary)'
    '#c2c6d6' = 'var(--text-primary)'
    '#8c909f' = 'var(--text-secondary)'
    '#8a94a8' = 'var(--text-secondary)'
    '#98a1b3' = 'var(--text-secondary)'
    '#9aa0c8' = 'var(--text-secondary)'
    '#6b6f80' = 'var(--text-secondary)'
    '#5b6373' = 'var(--text-tertiary)'
    '#5a6373' = 'var(--text-tertiary)'
    '#5a5d6a' = 'var(--text-tertiary)'
    '#6a7388' = 'var(--text-tertiary)'

    # Misc dark-bg semantic chips (info-dim / bear-dim shorthands)
    '#142a3a' = 'var(--info-dim)'
    '#3a1418' = 'var(--bear-dim)'
}

# Helper — case-insensitive literal replace counting hits.
function Replace-Hex {
    param([string]$Text, [string]$Hex, [string]$Token)
    $regex = [regex]::new([regex]::Escape($Hex), 'IgnoreCase')
    $count = $regex.Matches($Text).Count
    if ($count -eq 0) { return @{ text = $Text; count = 0 } }
    $newText = $regex.Replace($Text, $Token)
    return @{ text = $newText; count = $count }
}

$totalChanges = 0
$totalFiles = 0

foreach ($f in $Files) {
    $abs = Resolve-Path -LiteralPath $f -ErrorAction SilentlyContinue
    if (-not $abs) {
        Write-Output "[skip] file not found: $f"
        continue
    }
    $content = Get-Content -Raw -LiteralPath $abs.Path
    if (-not $content) { continue }

    $original = $content
    $fileChanges = 0
    $perHex = @{}
    foreach ($hex in $MAP.Keys) {
        $r = Replace-Hex -Text $content -Hex $hex -Token $MAP[$hex]
        if ($r.count -gt 0) {
            $content = $r.text
            $fileChanges += $r.count
            $perHex[$hex] = $r.count
        }
    }

    if ($fileChanges -eq 0) {
        Write-Output "[no-op]  $f"
        continue
    }

    $totalChanges += $fileChanges
    $totalFiles++
    Write-Output ""
    Write-Output "[$($(if ($DryRun) {'DRY-RUN'} else {'WRITE'}))]  $f  ->  $fileChanges substitution(s)"
    $perHex.GetEnumerator() | Sort-Object Value -Descending | ForEach-Object {
        Write-Output ("    {0,4}x  {1}  ->  {2}" -f $_.Value, $_.Key, $MAP[$_.Key])
    }

    if (-not $DryRun) {
        # CRITICAL: write as UTF-8 WITHOUT BOM. PowerShell 5.1's default
        # Set-Content encoding is system code page (often Windows-1252) which
        # SILENTLY corrupts non-ASCII chars (em-dash, ₹, →, ', ", etc.).
        # We use .NET WriteAllText with a BOM-less UTF8Encoding so browsers
        # and Python parsers don't choke on a BOM and unicode chars survive.
        [System.IO.File]::WriteAllText(
            $abs.Path,
            $content,
            (New-Object System.Text.UTF8Encoding $false)
        )
    }
}

Write-Output ""
Write-Output ("[done] {0} substitution(s) across {1} file(s){2}" -f $totalChanges, $totalFiles, $(if ($DryRun) {' (dry run, no writes)'} else {''}))
