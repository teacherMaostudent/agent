param(
    [string]$BaseUrl = "http://localhost:8080",
    [string]$Username = $env:ADMIN_USERNAME,
    [string]$Password = $env:ADMIN_PASSWORD,
    [string]$RequestFile = "ci/judge-request.json"
)

$ErrorActionPreference = "Stop"
if (-not (Test-Path -LiteralPath $RequestFile)) {
    throw "Judge request file does not exist: $RequestFile"
}
if ([string]::IsNullOrWhiteSpace($Username) -or [string]::IsNullOrWhiteSpace($Password)) {
    throw "ADMIN_USERNAME and ADMIN_PASSWORD are required"
}

$token = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("${Username}:${Password}"))
$headers = @{ Authorization = "Basic $token"; "Content-Type" = "application/json" }
$body = Get-Content -LiteralPath $RequestFile -Raw -Encoding UTF8

Write-Host "Running LLM Judge evaluation..."
$run = Invoke-RestMethod -Uri "$BaseUrl/admin/eval/judge-runs" -Method Post -Headers $headers -Body $body
Write-Host "Judge run: $($run.id), average score: $($run.metrics.averageScore)"

Write-Host "Evaluating deterministic quality gate..."
$gate = Invoke-RestMethod -Uri "$BaseUrl/admin/eval/judge-runs/$($run.id)/quality-gate" `
    -Method Post -Headers $headers -Body "{}"
$gate | ConvertTo-Json -Depth 10
if (-not $gate.passed) {
    Write-Error "LLM quality gate failed: $($gate.reasons -join '; ')"
    exit 1
}

Write-Host "LLM quality gate passed."
exit 0
