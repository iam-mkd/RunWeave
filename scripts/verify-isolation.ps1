[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
function Invoke-Checked {
    param([string] $Executable, [string[]] $Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Executable exited with code $LASTEXITCODE" }
}

$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$previousPython = $env:TEST_SANDBOX_PYTHON_IMAGE
$previousBrowser = $env:TEST_SANDBOX_BROWSER_IMAGE
Push-Location $project
try {
    Invoke-Checked uv @('sync', '--locked', '--extra', 'workflow', '--extra', 'eval')
    Invoke-Checked docker @('build', '-f', 'sandbox/Dockerfile.python', '-t', 'runweave-python:m15', '.')
    Invoke-Checked docker @('build', '-f', 'sandbox/Dockerfile.browser', '-t', 'runweave-browser:m15', '.')
    $env:TEST_SANDBOX_PYTHON_IMAGE = & docker image inspect --format '{{.Id}}' runweave-python:m15
    if ($LASTEXITCODE -ne 0) { throw 'Python image inspection failed' }
    $env:TEST_SANDBOX_BROWSER_IMAGE = & docker image inspect --format '{{.Id}}' runweave-browser:m15
    if ($LASTEXITCODE -ne 0) { throw 'Browser image inspection failed' }
    $python = (Resolve-Path '.venv/Scripts/python.exe').Path
    Invoke-Checked $python @('-m', 'pytest', 'tests/test_native_isolation.py', '-q', '-p', 'no:cacheprovider')
    Invoke-Checked $python @('scripts/check-isolation.py', '--python-image', $env:TEST_SANDBOX_PYTHON_IMAGE,
        '--browser-image', $env:TEST_SANDBOX_BROWSER_IMAGE, '--output', 'target/isolation/demo-proof.json')
} finally {
    $env:TEST_SANDBOX_PYTHON_IMAGE = $previousPython
    $env:TEST_SANDBOX_BROWSER_IMAGE = $previousBrowser
    Pop-Location
}
