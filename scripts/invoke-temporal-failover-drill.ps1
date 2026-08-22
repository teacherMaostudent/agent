[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [Parameter(Mandatory = $true)][string]$Namespace,
    [Parameter(Mandatory = $true)][string]$PrimaryAddress,
    [Parameter(Mandatory = $true)][string]$SecondaryAddress,
    [Parameter(Mandatory = $true)][string]$PrimaryCluster,
    [Parameter(Mandatory = $true)][string]$SecondaryCluster,
    [switch]$Execute,
    [switch]$KeepSecondaryActive
)

$ErrorActionPreference = "Stop"

function Invoke-Temporal {
    param([string]$Address, [string[]]$Arguments)
    & temporal --address $Address @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Temporal CLI failed against $Address with exit code $LASTEXITCODE."
    }
}

if (-not (Get-Command temporal -ErrorAction SilentlyContinue)) {
    throw "Temporal CLI is required. Install the version approved for your Temporal Cluster."
}
if ($PrimaryAddress -eq $SecondaryAddress -or $PrimaryCluster -eq $SecondaryCluster) {
    throw "Primary and secondary Temporal cluster identities must be different."
}

Write-Host "Preflight: describing replicated namespace from both clusters..." -ForegroundColor Cyan
Invoke-Temporal $PrimaryAddress @("operator", "namespace", "describe", "--namespace", $Namespace)
Invoke-Temporal $SecondaryAddress @("operator", "namespace", "describe", "--namespace", $Namespace)

if (-not $Execute) {
    Write-Host "Dry run complete. Re-run with -Execute after confirming both descriptions show the same Global Namespace and failover version." -ForegroundColor Yellow
    return
}

$failedOver = $false
try {
    if ($PSCmdlet.ShouldProcess($Namespace, "Fail over from $PrimaryCluster to $SecondaryCluster")) {
        Invoke-Temporal $PrimaryAddress @(
            "operator", "namespace", "update", "--namespace", $Namespace,
            "--active-cluster", $SecondaryCluster
        )
        $failedOver = $true
        Write-Host "Secondary cluster is active. Run the platform E2E and verify the secondary Runtime Worker consumes the same region Task Queue." -ForegroundColor Green
        Invoke-Temporal $SecondaryAddress @("operator", "namespace", "describe", "--namespace", $Namespace)
    }
}
finally {
    if ($failedOver -and -not $KeepSecondaryActive) {
        if ($PSCmdlet.ShouldProcess($Namespace, "Fail back to $PrimaryCluster")) {
            Invoke-Temporal $SecondaryAddress @(
                "operator", "namespace", "update", "--namespace", $Namespace,
                "--active-cluster", $PrimaryCluster
            )
            Write-Host "Failback completed; verify namespace state and SLO dashboards before closing the drill." -ForegroundColor Green
        }
    }
}
