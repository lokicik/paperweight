$ErrorActionPreference = 'Stop'
$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$explorerPython = Join-Path $projectDir '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $explorerPython)) {
    throw 'Run the setup commands in README.md to create the project virtual environment first.'
}
& $explorerPython -m paperweight @args
