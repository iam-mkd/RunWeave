[CmdletBinding()]
param(
    [string] $PostgresBin = 'C:\Program Files\PostgreSQL\18\bin',
    [switch] $ReleaseOnly,
    [string] $ReleaseReport = ''
)

$ErrorActionPreference = 'Stop'
function Invoke-Checked {
    param([string] $Executable, [string[]] $Arguments)
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$(Split-Path -Leaf $Executable) exited with code $LASTEXITCODE"
    }
}

$initdb = Join-Path $PostgresBin 'initdb.exe'
$pgCtl = Join-Path $PostgresBin 'pg_ctl.exe'
foreach ($binary in @($initdb, $pgCtl)) {
    if (-not (Test-Path -LiteralPath $binary -PathType Leaf)) {
        throw "PostgreSQL binary missing at $binary. Set -PostgresBin to your installation's bin directory."
    }
}

$project = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$tempRoot = (Resolve-Path ([IO.Path]::GetTempPath())).Path
$testRoot = Join-Path $tempRoot ('runweave-postgres-' + [guid]::NewGuid().ToString('N'))
$data = Join-Path $testRoot 'data'
$previousDsn = $env:TEST_RUNWEAVE_POSTGRES_URL
$previousReport = $env:TEST_RUNWEAVE_RELEASE_REPORT
$startAttempted = $false
$created = $false

Push-Location $project
try {
    Invoke-Checked uv @('sync', '--locked', '--no-editable', '--extra', 'workflow', '--extra', 'eval')
    $python = (Resolve-Path '.venv/Scripts/python.exe').Path
    Invoke-Checked uv @('pip', 'install', '--python', $python, '-r', 'requirements-durable.txt')

    New-Item -ItemType Directory -Path $testRoot | Out-Null
    $created = $true
    $passwordBytes = [byte[]]::new(24)
    $random = [Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($passwordBytes)
    } finally {
        $random.Dispose()
    }
    $password = [BitConverter]::ToString($passwordBytes).Replace('-', '')
    $passwordFile = Join-Path $testRoot 'password.txt'
    try {
        [IO.File]::WriteAllText($passwordFile, $password + "`n")
        Invoke-Checked $initdb @('-D', $data, '-U', 'runweave_test', '-A', 'scram-sha-256', "--pwfile=$passwordFile", '--locale=C', '-E', 'UTF8')
    } finally {
        if (Test-Path -LiteralPath $passwordFile) { Remove-Item -LiteralPath $passwordFile }
    }

    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 0)
    try {
        $listener.Start()
        $port = $listener.LocalEndpoint.Port
    } finally {
        $listener.Stop()
    }
    $startAttempted = $true
    $serverLog = Join-Path $testRoot 'postgres.log'
    try {
        Invoke-Checked $pgCtl @('-D', $data, '-l', $serverLog, '-o', "-h 127.0.0.1 -p $port", '-w', '-t', '15', 'start')
    } catch {
        if (Test-Path -LiteralPath $serverLog) {
            Get-Content -LiteralPath $serverLog -Tail 20
        }
        throw
    }
    $env:TEST_RUNWEAVE_POSTGRES_URL = "postgresql+asyncpg://runweave_test:${password}@127.0.0.1:${port}/postgres"
    if ($ReleaseReport) { $env:TEST_RUNWEAVE_RELEASE_REPORT = Join-Path $project $ReleaseReport }
    $testFiles = @('tests/test_postgres_release.py')
    if (-not $ReleaseOnly) {
        $testFiles += @('tests/test_postgres_recovery.py', 'tests/test_postgres_checkpoints.py',
            'tests/test_postgres_approvals.py', 'tests/test_postgres_memory.py', 'tests/test_postgres_coordination.py')
    }
    Invoke-Checked $python (@('-m', 'pytest') + $testFiles + @('-q', '--tb=short'))
} finally {
    $env:TEST_RUNWEAVE_POSTGRES_URL = $previousDsn
    $env:TEST_RUNWEAVE_RELEASE_REPORT = $previousReport
    try {
        $canRemove = $created
        if ($startAttempted) {
            & $pgCtl -D $data status
            if ($LASTEXITCODE -eq 0) {
                $canRemove = $false
                Invoke-Checked $pgCtl @('-D', $data, '-m', 'fast', '-w', '-t', '10', 'stop')
                $canRemove = $true
            } elseif ($LASTEXITCODE -ne 3) {
                $canRemove = $false
                Write-Warning "PostgreSQL status could not be verified. Test files remain in $testRoot."
            }
        }
        if ($canRemove) {
            $resolved = (Resolve-Path -LiteralPath $testRoot).Path
            if ($resolved -ne $testRoot -or (Get-Item -LiteralPath $resolved).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw "Unexpected test directory path. Files remain in $testRoot."
            }
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    } finally {
        Pop-Location
    }
}
