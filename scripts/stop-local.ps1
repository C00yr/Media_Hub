$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimeDir = Join-Path $projectRoot ".tmp-local-run"
$stateFile = Join-Path $runtimeDir "processes.json"

function Show-Notice([string]$message, [int]$seconds = 6) {
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup($message, $seconds, "PT Media Hub", 64)
    } catch {
        # The launcher is intentionally windowless.
    }
}

function Get-ChildProcessIds([int]$parentId) {
    $children = Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentId" -ErrorAction SilentlyContinue
    foreach ($child in $children) {
        Get-ChildProcessIds ([int]$child.ProcessId)
        [int]$child.ProcessId
    }
}

try {
    if (-not (Test-Path -LiteralPath $stateFile)) {
        Show-Notice "No PT Media Hub processes started by the launcher were found. Nothing was force-stopped."
        exit 0
    }

    $state = Get-Content -LiteralPath $stateFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $rootIds = @([int]$state.backend_pid, [int]$state.frontend_pid) | Where-Object { $_ -gt 0 }
    $allIds = New-Object System.Collections.Generic.List[int]
    foreach ($rootId in $rootIds) {
        foreach ($childId in @(Get-ChildProcessIds $rootId)) {
            if (-not $allIds.Contains([int]$childId)) {
                $allIds.Add([int]$childId)
            }
        }
        if (-not $allIds.Contains($rootId)) {
            $allIds.Add($rootId)
        }
    }

    foreach ($processId in $allIds) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    Remove-Item -LiteralPath $stateFile -Force -ErrorAction SilentlyContinue
    Show-Notice "PT Media Hub frontend and backend have been stopped."
} catch {
    Show-Notice "PT Media Hub could not be stopped cleanly: $($_.Exception.Message)`n`nRuntime files: $runtimeDir" 12
}
