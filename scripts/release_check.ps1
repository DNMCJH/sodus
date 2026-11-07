$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$python = if (Test-Path 'A:\Anaconda\python.exe') { 'A:\Anaconda\python.exe' } else { 'python' }
$temp = Join-Path ([IO.Path]::GetTempPath()) ('sodus-release-' + [guid]::NewGuid().ToString('N'))
$venv = Join-Path $temp 'venv'
New-Item -ItemType Directory -Path $temp | Out-Null
try {
  & $python -m build --sdist --wheel --outdir (Join-Path $temp 'dist') $root
  & $python -m venv $venv
  $vp = Join-Path $venv 'Scripts\python.exe'
  & $vp -m pip install --quiet (Get-ChildItem (Join-Path $temp 'dist') -Filter '*.whl' | Select-Object -First 1).FullName
  if ((& $vp -m sodus --version) -ne 'sodus 0.6.0') { throw 'sodus version mismatch' }
  if ((& $vp -m nodus --version) -ne 'nodus 0.6.0') { throw 'nodus version mismatch' }
  $stateHome = Join-Path $temp 'home'; New-Item -ItemType Directory $stateHome | Out-Null
  # A fresh home has no token; seed a syntactically valid one so doctor's home/db/agent
  # checks are the thing being tested, not a missing credential file.
  Set-Content -LiteralPath (Join-Path $stateHome 'token') -Value ('x' * 48) -NoNewline
  $env:SODUS_HOME = $stateHome
  $doctor = & $vp -m sodus doctor 2>&1
  $doctorText = ($doctor -join "`n")
  foreach ($must in @('PASS  home', 'PASS  database', 'PASS  agents')) {
    if ($doctorText -notmatch [regex]::Escape($must)) { throw "doctor missing '$must': $doctorText" }
  }
  $mcp = "{`"jsonrpc`":`"2.0`",`"id`":1,`"method`":`"initialize`",`"params`":{`"protocolVersion`":`"2024-11-05`",`"capabilities`":{},`"clientInfo`":{`"name`":`"release-check`",`"version`":`"1`"}}}`n{`"jsonrpc`":`"2.0`",`"id`":2,`"method`":`"tools/list`"}`n"
  $mcpOutput = $mcp | & $vp -m sodus mcp
  if ($LASTEXITCODE -ne 0) { throw 'MCP check failed' }
  $messages = @($mcpOutput | ForEach-Object { $_ | ConvertFrom-Json })
  if ($messages[0].result.serverInfo.name -ne 'sodus' -or $messages[1].result.tools.Count -lt 10) { throw 'MCP initialize/tools-list contract failed' }
  # The venv has no pytest; run the contract tests with the build interpreter
  # from the worktree so `nodus`/`sodus` resolve to this tree.
  Push-Location $root
  try { & $python -m pytest -q tests/test_sodus_compat.py tests/test_openapi.py }
  finally { Pop-Location }
  if ($LASTEXITCODE -ne 0) { throw 'release tests failed' }
  Write-Output 'release_check: PASS'
  exit 0
} finally { Remove-Item -LiteralPath $temp -Recurse -Force -ErrorAction SilentlyContinue }
