# Every command surface must load and print help.
#
# argparse only fails at build_parser(), which is why a broken new subcommand
# can pass the unit tests and still make the whole CLI unusable. This walks the
# real parser instead of a hard-coded list, so a new command is covered the day
# it is added.
#
#   pwsh -NoProfile -File scripts/smoke_cli.ps1
#
# Read-only: it never dispatches a command, only --help.

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    # The console script is the thing being tested, but a fresh checkout (or CI)
    # may not have it on PATH yet; the entry point is the same code either way.
    $exe = 'nodus'
    $prefix = @()
    if (-not (Get-Command nodus -ErrorAction SilentlyContinue)) {
        $exe = 'python'
        $prefix = @('nodus.py')
        Write-Host "nodus not on PATH; using: python nodus.py"
    }
    $surfaces = python -c @"
from nodus.cli import build_parser
p = build_parser()
sub = [a for a in p._actions if a.dest == 'cmd'][0]
for name, sp in sub.choices.items():
    inner = [a for a in sp._actions if a.dest == 'action']
    if inner:
        for act in inner[0].choices:
            print(f'{name} {act}')
    else:
        print(name)
"@
    if ($LASTEXITCODE -ne 0) { throw "could not enumerate commands" }
    $fail = 0
    $checked = 0
    foreach ($surface in $surfaces) {
        $checked++
        $parts = $surface -split ' '
        $output = & $exe @($prefix + $parts + '--help') 2>&1
        if ($LASTEXITCODE -ne 0) {
            $fail++
            Write-Host "FAIL  nodus $surface" -ForegroundColor Red
            Write-Host $output
        }
    }
    Write-Host "checked $checked command surfaces, failures=$fail"
    exit $(if ($fail -eq 0) { 0 } else { 1 })
}
finally {
    Pop-Location
}
