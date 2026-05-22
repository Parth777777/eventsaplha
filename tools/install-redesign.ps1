# install-redesign.ps1
#
# Bulk applies two fixes across every HTML page in app/:
#   1. Replace the broken localStorage key 'tw-theme' with the canonical
#      'tickwave:theme' so first-paint theme detection matches theme.js.
#   2. Insert <link rel="stylesheet" href="./shared/css/redesign.css">
#      directly after the style.css link, so the premium override layer
#      hits the first paint (no FOUC waiting for bootstrap.js to inject).
#   3. Same for ./shared/css/disclosures.css (SEBI footer + compliance modal
#      styles) so the per-card disclaimer chips don't flash unstyled.
#
# Idempotent — runs safely multiple times. Reports per-file changes.

[CmdletBinding()]
param([switch]$DryRun)

$ErrorActionPreference = 'Stop'
$root = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $root

$htmlFiles = Get-ChildItem -Path 'app' -Filter '*.html' -ErrorAction SilentlyContinue
if (-not $htmlFiles) {
    Write-Output "[install-redesign] no HTML files in app/ — aborting"
    exit 1
}

$totals = @{ themeFix = 0; redesignAdd = 0; disclosuresAdd = 0; filesTouched = 0 }

foreach ($f in $htmlFiles) {
    $rel = $f.FullName.Substring($root.Path.Length + 1).Replace('\','/')
    $orig = Get-Content -Raw -LiteralPath $f.FullName
    if (-not $orig) { continue }
    $content = $orig
    $touched = $false
    $perFile = @{ themeFix = 0; redesignAdd = $false; disclosuresAdd = $false }

    # Fix 1 — theme key. Match both single-quoted and double-quoted forms.
    if ($content -match "'tw-theme'") {
        $count = ([regex]::Matches($content, "'tw-theme'")).Count
        $content = $content -replace "'tw-theme'", "'tickwave:theme'"
        $perFile.themeFix = $count
        $totals.themeFix += $count
        $touched = $true
    }

    # Fix 2 — inject redesign.css link right after the first style.css link.
    # Skip if already present (idempotent).
    if ($content -notmatch 'redesign\.css') {
        $linkAnchor = '<link rel="stylesheet" href="./shared/css/style.css">'
        if ($content.Contains($linkAnchor)) {
            $insertion = $linkAnchor + "`n    <link rel=`"stylesheet`" href=`"./shared/css/redesign.css`">"
            $content = $content.Replace($linkAnchor, $insertion)
            $perFile.redesignAdd = $true
            $totals.redesignAdd++
            $touched = $true
        }
    }

    # Fix 3 — inject disclosures.css link right after style.css link too.
    if ($content -notmatch 'disclosures\.css') {
        $linkAnchor = '<link rel="stylesheet" href="./shared/css/style.css">'
        if ($content.Contains($linkAnchor)) {
            $insertion = $linkAnchor + "`n    <link rel=`"stylesheet`" href=`"./shared/css/disclosures.css`">"
            $content = $content.Replace($linkAnchor, $insertion)
            $perFile.disclosuresAdd = $true
            $totals.disclosuresAdd++
            $touched = $true
        }
    }

    if (-not $touched) {
        Write-Output "[no-op]  $rel"
        continue
    }

    $totals.filesTouched++
    $tag = if ($DryRun) { 'DRY-RUN' } else { 'WRITE' }
    Write-Output "[$tag]  $rel"
    if ($perFile.themeFix -gt 0)    { Write-Output "    themeKey: $($perFile.themeFix) replacement(s)" }
    if ($perFile.redesignAdd)        { Write-Output "    +redesign.css link" }
    if ($perFile.disclosuresAdd)     { Write-Output "    +disclosures.css link" }

    if (-not $DryRun) {
        # CRITICAL: write as UTF-8 WITHOUT BOM. PowerShell 5.1 Set-Content
        # defaults to system code page (Windows-1252) and silently mangles
        # non-ASCII chars (em-dash, ₹, →, curly quotes). Using .NET
        # WriteAllText with BOM-less UTF8Encoding is the only safe path.
        [System.IO.File]::WriteAllText(
            $f.FullName,
            $content,
            (New-Object System.Text.UTF8Encoding $false)
        )
    }
}

Write-Output ""
Write-Output ("[done] {0} file(s) touched · theme-key fixes: {1} · redesign.css added: {2} · disclosures.css added: {3}{4}" -f `
    $totals.filesTouched, $totals.themeFix, $totals.redesignAdd, $totals.disclosuresAdd,
    $(if ($DryRun) { ' (dry run)' } else { '' }))
