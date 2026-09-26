[CmdletBinding()]
param(
    [ValidateRange(1, 60)]
    [int]$TimeoutSeconds = 5
)

$ErrorActionPreference = 'Stop'
$expectedExe = [IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot '..\shellthumb\warmshell.exe')
)
$launchMutex = [Threading.Mutex]::new(
    $false,
    'Local\TeleDriveWarmshellLaunch'
)
$ownsMutex = $false

try {
    try {
        $ownsMutex = $launchMutex.WaitOne(
            [TimeSpan]::FromSeconds($TimeoutSeconds)
        )
    } catch [Threading.AbandonedMutexException] {
        # The previous bridge died during process creation.  WaitOne grants the
        # abandoned mutex to this thread, so it is safe to continue inspecting.
        $ownsMutex = $true
    }

    if (-not $ownsMutex) {
        [Console]::Error.WriteLine(
            'Restart could not close the warmshell launch gate; the running ' +
            'bridge was left untouched.'
        )
        exit 5
    }

    try {
        $warmers = @(
            Get-CimInstance Win32_Process -Filter "Name = 'warmshell.exe'"
        )
        $processesById = @{}
        Get-CimInstance Win32_Process | ForEach-Object {
            $processesById[[int]$_.ProcessId] = $_
        }
    } catch {
        [Console]::Error.WriteLine(
            "Could not inspect warmshell processes: $($_.Exception.Message)"
        )
        exit 4
    }

    # Only terminate this bridge's live children.  A missing or unrelated
    # parent means the warmer is already orphaned; attempting to force it out
    # of a WinFsp PageIn wait can leave the console and mount in a worse state.
    # Fail before touching either warmer or bridge so restart remains atomic.
    $unsafe = @()
    foreach ($warmer in $warmers) {
        $parent = $processesById[[int]$warmer.ParentProcessId]
        $warmerExe = if ($warmer.ExecutablePath) {
            [IO.Path]::GetFullPath($warmer.ExecutablePath)
        } else {
            ''
        }
        $hasBridgeParent = $null -ne $parent `
            -and $parent.Name -like 'python*' `
            -and $parent.CommandLine -like '*bridge.py*'
        if ($warmerExe -ne $expectedExe -or -not $hasBridgeParent) {
            $unsafe += $warmer
        }
    }

    if ($unsafe.Count -ne 0) {
        $ids = ($unsafe | ForEach-Object { $_.ProcessId }) -join ', '
        [Console]::Error.WriteLine(
            "Refusing to restart: orphaned or unrelated warmshell process(es) " +
            "$ids must be cleared by reboot; the running bridge was left untouched."
        )
        exit 2
    }

    $warmerIds = @($warmers | ForEach-Object { [int]$_.ProcessId })
    foreach ($processId in $warmerIds) {
        Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ($warmerIds.Count -eq 0) {
            $leftWarmers = @()
        } else {
            try {
                $leftWarmers = @(
                    Get-CimInstance Win32_Process -Filter "Name = 'warmshell.exe'" |
                        Where-Object { $warmerIds -contains [int]$_.ProcessId }
                )
            } catch {
                [Console]::Error.WriteLine(
                    "Could not verify warmshell shutdown: $($_.Exception.Message)"
                )
                exit 4
            }
        }
        if ($leftWarmers.Count -eq 0) {
            break
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    if ($leftWarmers.Count -ne 0) {
        $leftIds = ($leftWarmers | ForEach-Object { $_.ProcessId }) -join ', '
        [Console]::Error.WriteLine(
            "warmshell process(es) $leftIds did not stop within " +
            "$TimeoutSeconds seconds; the running bridge was left untouched."
        )
        exit 3
    }

    # Keep the launch mutex until every bridge process is gone.  A warmer that
    # was about to spawn either appeared in the snapshot above or is blocked on
    # this mutex and dies with its parent; there is no orphan-producing gap.
    try {
        $bridges = @(
            Get-CimInstance Win32_Process | Where-Object {
                $_.Name -like 'python*' -and $_.CommandLine -like '*bridge.py*'
            }
        )
        $bridgeIds = @($bridges | ForEach-Object { [int]$_.ProcessId })
        foreach ($processId in $bridgeIds) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
        }
    } catch {
        [Console]::Error.WriteLine(
            "Could not stop bridge processes: $($_.Exception.Message)"
        )
        exit 6
    }

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $leftBridges = @(
            $bridgeIds | Where-Object {
                $null -ne (Get-Process -Id $_ -ErrorAction SilentlyContinue)
            }
        )
        if ($leftBridges.Count -eq 0) {
            exit 0
        }
        Start-Sleep -Milliseconds 200
    } while ([DateTime]::UtcNow -lt $deadline)

    [Console]::Error.WriteLine(
        "Bridge process(es) $($leftBridges -join ', ') did not stop."
    )
    exit 6
} finally {
    if ($ownsMutex) {
        [void]$launchMutex.ReleaseMutex()
    }
    $launchMutex.Dispose()
}
