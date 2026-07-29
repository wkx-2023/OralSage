param(
    [switch]$Restart,
    [int]$BackendPort = 8000,
    [int]$BridgePort = 8787
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$bridgeRoot = Join-Path $projectRoot "clawbridge"
$envPath = Join-Path $projectRoot ".env"
$logsDir = Join-Path $projectRoot "logs"
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"

function Read-DotEnvValue {
    param([string]$Name)

    if (-not (Test-Path -LiteralPath $envPath)) {
        return ""
    }
    $pattern = "^\s*" + [regex]::Escape($Name) + "\s*="
    $line = Get-Content -LiteralPath $envPath -Encoding utf8 |
        Where-Object { $_ -match $pattern } |
        Select-Object -Last 1
    if (-not $line) {
        return ""
    }
    return (($line -split "=", 2)[1]).Trim().Trim('"').Trim("'")
}

function Get-ListenerProcessId {
    param([int]$Port)

    $listener = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($null -eq $listener) {
        return $null
    }
    return [int]$listener.OwningProcess
}

function Test-Endpoint {
    param([string]$Uri)

    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri $Uri -TimeoutSec 3
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 300
    } catch {
        return $false
    }
}

function Wait-Endpoint {
    param(
        [string]$Uri,
        [int]$TimeoutSeconds = 40
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Endpoint -Uri $Uri) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Stop-PortListener {
    param([int]$Port)

    $processId = Get-ListenerProcessId -Port $Port
    if ($null -eq $processId) {
        return
    }
    Stop-Process -Id $processId -Force
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline) {
        if ($null -eq (Get-ListenerProcessId -Port $Port)) {
            return
        }
        Start-Sleep -Milliseconds 250
    }
    throw "Port $Port did not become available after stopping process $processId."
}

if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Missing $envPath. Create it from .env.example first."
}

$backendSecret = Read-DotEnvValue -Name "ORALSAGE_CLAWBRIDGE_INTERNAL_SECRET"
$bridgeSecret = Read-DotEnvValue -Name "CLAWBRIDGE_INTERNAL_SECRET"
if (-not $backendSecret -or -not $bridgeSecret) {
    throw "Both ORALSAGE_CLAWBRIDGE_INTERNAL_SECRET and CLAWBRIDGE_INTERNAL_SECRET must be configured in .env."
}
if ($backendSecret -cne $bridgeSecret) {
    throw "The backend and ClawBridge internal secrets in .env do not match."
}

$pythonPath = Join-Path $projectRoot "env\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
}
$nodePath = (Get-Command node -ErrorAction Stop).Source
if (-not (Test-Path -LiteralPath (Join-Path $bridgeRoot "node_modules\openclaw"))) {
    throw "ClawBridge dependencies are missing. Run npm install in $bridgeRoot."
}

New-Item -ItemType Directory -Path $logsDir -Force | Out-Null

if ($Restart) {
    Stop-PortListener -Port $BridgePort
    Stop-PortListener -Port $BackendPort
}

$backendPid = Get-ListenerProcessId -Port $BackendPort
if ($null -eq $backendPid) {
    $backendProcess = Start-Process `
        -FilePath $pythonPath `
        -ArgumentList @("-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", "$BackendPort") `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logsDir "backend-$timestamp.out.log") `
        -RedirectStandardError (Join-Path $logsDir "backend-$timestamp.err.log") `
        -PassThru
    $backendPid = $backendProcess.Id
}

$backendHealth = "http://127.0.0.1:$BackendPort/api/system/health"
if (-not (Wait-Endpoint -Uri $backendHealth -TimeoutSeconds 45)) {
    throw "OralSage backend did not become healthy at $backendHealth. Check logs/backend-$timestamp.err.log."
}

$bridgePid = Get-ListenerProcessId -Port $BridgePort
if ($null -eq $bridgePid) {
    $bridgeProcess = Start-Process `
        -FilePath $nodePath `
        -ArgumentList @("src/index.mjs") `
        -WorkingDirectory $bridgeRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $logsDir "clawbridge-$timestamp.out.log") `
        -RedirectStandardError (Join-Path $logsDir "clawbridge-$timestamp.err.log") `
        -PassThru
    $bridgePid = $bridgeProcess.Id
}

$bridgeReady = "http://127.0.0.1:$BridgePort/ready"
if (-not (Wait-Endpoint -Uri $bridgeReady -TimeoutSeconds 45)) {
    throw "ClawBridge did not become ready at $bridgeReady. Check logs/clawbridge-$timestamp.out.log and .err.log."
}

Write-Host "OralSage backend ready: http://127.0.0.1:$BackendPort (PID $backendPid)"
Write-Host "ClawBridge ready:       http://127.0.0.1:$BridgePort (PID $bridgePid)"
Write-Host "Open OralSage:          http://127.0.0.1:$BackendPort/"
