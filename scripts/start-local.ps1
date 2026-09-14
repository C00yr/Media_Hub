$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $projectRoot "backend"
$frontendDir = Join-Path $projectRoot "frontend"
$runtimeDir = Join-Path $projectRoot ".tmp-local-run"
$stateFile = Join-Path $runtimeDir "processes.json"
$backendOut = Join-Path $runtimeDir "backend.out.log"
$backendErr = Join-Path $runtimeDir "backend.err.log"
$frontendOut = Join-Path $runtimeDir "frontend.out.log"
$frontendErr = Join-Path $runtimeDir "frontend.err.log"
$appUrl = "http://localhost:5173"
$backendHealthUrl = "http://127.0.0.1:8001/health"

function Show-Notice([string]$message, [int]$seconds = 8) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup($message, $seconds, "PT Media Hub", 64)
    } catch {
        # The launcher is intentionally windowless. Log-only fallback is enough.
    }
}

function Test-ProcessAlive([int]$processId) {
    return $null -ne (Get-Process -Id $processId -ErrorAction SilentlyContinue)
}

function Test-Http([string]$uri) {
    try {
        $response = Invoke-WebRequest -Uri $uri -UseBasicParsing -TimeoutSec 2
        return $response.StatusCode -ge 200 -and $response.StatusCode -lt 500
    } catch {
        return $false
    }
}

function Test-Port([int]$port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $async = $client.BeginConnect("127.0.0.1", $port, $null, $null)
        if (-not $async.AsyncWaitHandle.WaitOne(400)) {
            return $false
        }
        $client.EndConnect($async)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

try {
    New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

    if (Test-Path -LiteralPath $stateFile) {
        try {
            $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
            $backendAlive = Test-ProcessAlive ([int]$state.backend_pid)
            $frontendAlive = Test-ProcessAlive ([int]$state.frontend_pid)
            if ($backendAlive -and $frontendAlive) {
                Start-Process $appUrl
                Show-Notice "PT Media Hub is already running. The web page has been opened."
                exit 0
            }
        } catch {
            # A stale state file is replaced after port safety checks below.
        }
    }

    if ((Test-Port 8001) -or (Test-Port 5173)) {
        Show-Notice "Port 8001 or 5173 is already in use. Startup was cancelled to avoid affecting another program. Check .tmp-local-run for logs."
        exit 1
    }

    $pythonExe = Join-Path $backendDir ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonExe)) {
        $py = Get-Command py.exe -ErrorAction SilentlyContinue
        if (-not $py) {
            throw "Python launcher py.exe was not found."
        }
        $venv = Start-Process -FilePath $py.Source `
            -ArgumentList @("-m", "venv", (Join-Path $backendDir ".venv")) `
            -WorkingDirectory $backendDir -WindowStyle Hidden -Wait -PassThru `
            -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
        if ($venv.ExitCode -ne 0) {
            throw "Failed to create the Python virtual environment."
        }
        $install = Start-Process -FilePath $pythonExe `
            -ArgumentList @("-m", "pip", "install", "-e", ".") `
            -WorkingDirectory $backendDir -WindowStyle Hidden -Wait -PassThru `
            -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr
        if ($install.ExitCode -ne 0) {
            throw "Failed to install backend dependencies."
        }
    }

    $npm = Get-Command npm.cmd -ErrorAction SilentlyContinue
    if (-not $npm) {
        throw "npm.cmd was not found. Install Node.js LTS first."
    }
    if (-not (Test-Path -LiteralPath (Join-Path $frontendDir "node_modules"))) {
        $npmInstall = Start-Process -FilePath $npm.Source `
            -ArgumentList @("ci") -WorkingDirectory $frontendDir `
            -WindowStyle Hidden -Wait -PassThru `
            -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr
        if ($npmInstall.ExitCode -ne 0) {
            throw "Failed to install frontend dependencies."
        }
    }

    # Force local isolation even if the parent Windows environment has NAS
    # deployment variables. Local data and secrets remain under data/local.
    $env:APP_RUNTIME_PROFILE = "local"
    $env:APP_TIMEZONE = "Asia/Shanghai"
    Remove-Item Env:APP_DATA_DIR -ErrorAction SilentlyContinue
    Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
    Remove-Item Env:APP_RUNTIME_SECRETS_FILE -ErrorAction SilentlyContinue

    $backend = Start-Process -FilePath $pythonExe `
        -ArgumentList @("-m", "uvicorn", "app.main:app", "--reload", "--host", "127.0.0.1", "--port", "8001") `
        -WorkingDirectory $backendDir -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $backendOut -RedirectStandardError $backendErr

    $frontend = Start-Process -FilePath $npm.Source `
        -ArgumentList @("run", "dev") -WorkingDirectory $frontendDir `
        -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $frontendOut -RedirectStandardError $frontendErr

    @{
        backend_pid = $backend.Id
        frontend_pid = $frontend.Id
        started_at = (Get-Date).ToString("o")
        project_root = $projectRoot
    } | ConvertTo-Json | Set-Content -LiteralPath $stateFile -Encoding UTF8

    $ready = $false
    $deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $deadline) {
        if (-not (Test-ProcessAlive $backend.Id) -or -not (Test-ProcessAlive $frontend.Id)) {
            break
        }
        if ((Test-Http $backendHealthUrl) -and (Test-Http $appUrl)) {
            $ready = $true
            break
        }
        Start-Sleep -Milliseconds 750
    }

    if (-not $ready) {
        throw "The frontend or backend did not become ready within 60 seconds."
    }

    Start-Process $appUrl
    Show-Notice "PT Media Hub is ready and the web page has been opened. Use the close launcher to stop it."
} catch {
    $message = $_.Exception.Message
    Show-Notice "PT Media Hub failed to start: $message`n`nLogs: $runtimeDir" 15
}
