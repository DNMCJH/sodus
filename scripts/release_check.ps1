$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = if (Test-Path 'A:\Anaconda\python.exe') { 'A:\Anaconda\python.exe' } else { 'python' }
$temp = Join-Path ([IO.Path]::GetTempPath()) ('sodus-release-' + [guid]::NewGuid().ToString('N'))
$venv = Join-Path $temp 'venv'
New-Item -ItemType Directory -Path $temp | Out-Null
function Invoke-Checked([string]$File, [string[]]$ArgList) {
  # NOTE: do not name this parameter $Args - it collides with PowerShell's
  # automatic $args variable and silently unbinds, running the file bare.
  & $File @ArgList
  if ($LASTEXITCODE -ne 0) { throw "command failed ($LASTEXITCODE): $File $($ArgList -join ' ')" }
}
try {
  $version = ((Get-Content (Join-Path $root 'pyproject.toml') | Select-String '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value)
  Invoke-Checked $python @('-m','build','--sdist','--wheel','--outdir',(Join-Path $temp 'dist'),$root)
  Invoke-Checked $python @('-m','venv',$venv)
  $vp = Join-Path $venv 'Scripts\python.exe'
  Invoke-Checked $vp @('-m','pip','install','--quiet',(Get-ChildItem (Join-Path $temp 'dist') -Filter '*.whl' | Select-Object -First 1).FullName)
  if ((& $vp -m sodus --version) -ne "sodus $version") { throw 'sodus version mismatch' }
  if ((& $vp -m nodus --version) -ne "nodus $version") { throw 'nodus version mismatch' }
  $stateHome = Join-Path $temp 'home'; New-Item -ItemType Directory $stateHome | Out-Null
  # A fresh home has no token; seed a syntactically valid one so doctor's home/db/agent
  # checks are the thing being tested, not a missing credential file.
  Set-Content -LiteralPath (Join-Path $stateHome 'token') -Value ('x' * 48) -NoNewline
  $pyCounts = @'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("select count(*) from task").fetchone()[0])
print(c.execute("select count(*) from run").fetchone()[0])
'@
  $pyAfter = @'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
print(c.execute("select value from meta where key='schema_version'").fetchone()[0])
print(c.execute("select count(*) from task").fetchone()[0])
print(c.execute("select count(*) from run").fetchone()[0])
'@
  $sourceDb = Join-Path $env:USERPROFILE '.nodus\nodus.sqlite3'
  if (Test-Path $sourceDb) {
    Copy-Item $sourceDb (Join-Path $stateHome 'nodus.sqlite3')
    $before = & $python -c $pyCounts (Join-Path $stateHome 'nodus.sqlite3')
  }
  # The schema version is a property of the release being tested, not a literal
  # that has to be edited every time a migration lands.
  $expectedSchema = (& $vp -c "from nodus import db; print(db.SCHEMA_VERSION)").Trim()
  if (-not $expectedSchema) { throw 'could not read db.SCHEMA_VERSION' }
  $env:SODUS_HOME = $stateHome
  $doctor = & $vp -m sodus doctor 2>&1; if ($LASTEXITCODE -ne 0) { throw 'doctor failed' }
  $doctorText = ($doctor -join "`n")
  foreach ($must in @('PASS  home', 'PASS  database', 'PASS  agents')) {
    if ($doctorText -notmatch [regex]::Escape($must)) { throw "doctor missing '$must': $doctorText" }
  }
  if (Test-Path (Join-Path $stateHome 'nodus.sqlite3')) {
    $after = & $python -c $pyAfter (Join-Path $stateHome 'nodus.sqlite3')
    if ($after[0] -ne $expectedSchema -or $after[1] -ne $before[0] -or $after[2] -ne $before[1]) { throw "DB upgrade smoke failed (schema $($after[0]) != $expectedSchema, or task/run counts changed)" }
  }
  $mcp = "{`"jsonrpc`":`"2.0`",`"id`":1,`"method`":`"initialize`",`"params`":{`"protocolVersion`":`"2024-11-05`",`"capabilities`":{},`"clientInfo`":{`"name`":`"release-check`",`"version`":`"1`"}}}`n{`"jsonrpc`":`"2.0`",`"id`":2,`"method`":`"tools/list`"}`n"
  $mcpOutput = $mcp | & $vp -m sodus mcp
  if ($LASTEXITCODE -ne 0) { throw 'MCP check failed' }
  $messages = @($mcpOutput | ForEach-Object { $_ | ConvertFrom-Json })
  if ($messages[0].result.serverInfo.name -ne 'sodus' -or $messages[1].result.tools.Count -lt 10) { throw 'MCP initialize/tools-list contract failed' }
  # Do not leak the isolated SODUS_HOME into the test process: tests build
  # their own temp homes and a stray env var breaks config migration fixtures.
  Remove-Item Env:SODUS_HOME -ErrorAction SilentlyContinue
  # The venv has no pytest; run the contract tests with the build interpreter
  # from the worktree so `nodus`/`sodus` resolve to this tree.
  Push-Location $root
  try { Invoke-Checked $python @('-m','pytest','-q') }
  finally { Pop-Location }
  Write-Output 'release_check: PASS'
  exit 0
} finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
