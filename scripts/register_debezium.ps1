param(
    [string]$ConnectUrl = "http://localhost:8083",
    [string]$ConfigPath = "deploy/debezium/platform-outbox.json",
    [int]$TimeoutSeconds = 120
)

# Delegate to the same idempotent registrar used by Compose.  Keeping one
# implementation prevents local recovery commands from drifting from deploy.
$env:KAFKA_CONNECT_URL = $ConnectUrl
$env:DEBEZIUM_CONFIG_PATH = (Resolve-Path -LiteralPath $ConfigPath)
$env:DEBEZIUM_REGISTER_TIMEOUT_SECONDS = $TimeoutSeconds
python deploy/debezium/register.py
