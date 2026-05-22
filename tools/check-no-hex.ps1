# check-no-hex.ps1 — Token-contract lint.
#
# Scans app/shared/js/*.js and inline styles in app/*.html for raw hex
# color literals. Enforces the design-token contract documented in
# app/shared/css/tokens.css: widget JS and HTML inline styles must read
# colors via `var(--token)` or `ThemeTokens.color('--token')`. Raw hex
# bypasses the theme system and was the root cause of the "black spots
# in light mode" feedback (2026-05-17).
#
# Exits non-zero with a summary of violations. Allowlisted files:
#   - app/shared/js/theme-tokens.js  (bridge implementation)
#   - app/shared/css/tokens.css      (token definitions)
#   - app/shared/js/advanced-chart.js BRAND constant (brand identity)
#   - Brand-asset .svg files
#
# Usage:
#   pwsh tools/check-no-hex.ps1            # report violations, exit 1 on any
#   pwsh tools/check-no-hex.ps1 -Verbose   # show every violation line
#
# Designed to be wired as a pre-commit hook later.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

# Files exempt from the no-hex rule — these own the brand/token vocabulary.
$allowlist = @(
  'app/shared/css/tokens.css',
  'app/shared/js/theme-tokens.js'
)

# Targets to scan
$jsFiles  = Get-ChildItem -Path 'app/shared/js' -Filter '*.js' -ErrorAction SilentlyContinue
$htmFiles = Get-ChildItem -Path 'app' -Filter '*.html' -ErrorAction SilentlyContinue

# Hex literal pattern (3, 4, 6, or 8 hex digits prefixed with #)
$hexRegex = '#[0-9a-fA-F]{3,8}\b'

$violations = @()

foreach ($f in @($jsFiles) + @($htmFiles)) {
    $rel = $f.FullName.Substring($root.Path.Length + 1).Replace('\','/')
    if ($allowlist -contains $rel) { continue }

    $lineNo = 0
    foreach ($line in (Get-Content -LiteralPath $f.FullName)) {
        $lineNo++
        # In .html files, only flag inline style="..." occurrences; raw <svg>
        # path fills (brand assets) are allowed.
        $matches = [regex]::Matches($line, $hexRegex)
        if (-not $matches.Count) { continue }

        # Filter: ignore lines clearly inside a comment-only context.
        $trim = $line.TrimStart()
        if ($trim.StartsWith('//') -or $trim.StartsWith('*') -or $trim.StartsWith('/*')) { continue }

        # advanced-chart.js: BRAND constant is allowed (brand identity).
        if ($rel -eq 'app/shared/js/advanced-chart.js' -and $line -match 'BRAND\s*=|BRAND\.\w+') { continue }

        # .html: only flag if hex appears inside a style="..." attribute.
        if ($f.Extension -eq '.html') {
            if ($line -notmatch 'style\s*=') { continue }
        }

        foreach ($m in $matches) {
            $violations += [pscustomobject]@{
                File   = $rel
                Line   = $lineNo
                Hex    = $m.Value
                Source = $line.Trim()
            }
        }
    }
}

if (-not $violations.Count) {
    Write-Output "[check-no-hex] OK - no raw hex literals found."
    exit 0
}

$byFile = $violations | Group-Object File | Sort-Object Count -Descending
Write-Output ""
Write-Output "[check-no-hex] $($violations.Count) violation(s) across $($byFile.Count) file(s):"
Write-Output ""
foreach ($g in $byFile) {
    Write-Output ("  {0,4}  {1}" -f $g.Count, $g.Name)
}

if ($VerbosePreference -eq 'Continue') {
    Write-Output ""
    Write-Output "Details:"
    foreach ($v in $violations | Select-Object -First 80) {
        Write-Output ("  {0}:{1}  {2}" -f $v.File, $v.Line, $v.Hex)
    }
    if ($violations.Count -gt 80) {
        Write-Output ("  ... and {0} more (re-run with -Verbose to see fewer at a time)" -f ($violations.Count - 80))
    }
}

Write-Output ""
Write-Output "Fix: replace literals with var(--token) from app/shared/css/tokens.css"
Write-Output "     or ThemeTokens.color('--token') in JS contexts."
exit 1
