param(
    [string]$ConnectUrl = "http://localhost:8083",
    [string]$ConfigPath = "deploy/debezium/platform-outbox.json"
)

$resolved = Resolve-Path -LiteralPath $ConfigPath
$content = Get-Content -LiteralPath $resolved -Raw
$content = $content.Replace('${POSTGRES_USER}', $env:POSTGRES_USER)
$content = $content.Replace('${POSTGRES_PASSWORD}', $env:POSTGRES_PASSWORD)
Invoke-RestMethod -Method Post -Uri "$ConnectUrl/connectors" -ContentType "application/json" -Body $content
