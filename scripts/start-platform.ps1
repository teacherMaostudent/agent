[CmdletBinding()]
param(
    [ValidateSet("Start", "Stop", "Restart", "Status", "Logs")]
    [string]$Action = "Start",
    [switch]$NoBuild,
    [switch]$SkipE2E,
    [switch]$WithLabs,
    # 本地身份联调是可选覆盖层：启用后 BFF 使用真实 OIDC/PKCE，而不再信任开发 Header。
    [switch]$WithIdentity,
    [ValidateRange(30, 900)]
    [int]$TimeoutSeconds = 240
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repositoryRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$composeFile = Join-Path $repositoryRoot "compose.platform.yaml"
$identityComposeFile = Join-Path $repositoryRoot "compose.identity.yaml"
$projectName = "agent-platform"
$composeFileArguments = @("-f", $composeFile)
if ($WithIdentity) {
    if (-not (Test-Path -LiteralPath $identityComposeFile)) {
        throw "未找到本地身份覆盖文件：$identityComposeFile"
    }
    $composeFileArguments += @("-f", $identityComposeFile)
}

# 七个逻辑服务之外，PostgreSQL、Redis 和摄取工作负载属于其运行依赖，必须一起启动，
# 但不会被误计为新的平台治理服务。实验室默认不进入在线联调链路。
$coreServices = @(
    "postgres",
    "redis",
    "agent-control-plane",
    "agent-governance",
    "agent-governance-worm-worker",
    "llm-gateway",
    "rag-query-api",
    "agent-context-service",
    "ingestion-api",
    "ingestion-worker",
    "tool-gateway",
    "agent-runtime",
    "agent-runtime-connector-relay",
    "agent-runtime-artifact-ingestion-relay",
    # BFF 是浏览器访问 Runtime 的同源投影层，不属于七个领域服务，但必须和 Workspace 一起启动。
    "agent-web-bff"
)
# 本地使用共享开发卷上的轮询 Worker；生产模板构建独立 Temporal Worker Target，
# 两种入口不会因同名镜像而在缺少 Temporal 时反复重启。
$labServices = @("model-lab", "agent-lab")
$identityServices = if ($WithIdentity) { @("keycloak") } else { @() }
$managedServices = if ($WithLabs) { $coreServices + $labServices + $identityServices } else { $coreServices + $identityServices }

$healthChecks = [ordered]@{
    "Control Plane"   = "http://127.0.0.1:9002/health/ready"
    "Governance"      = "http://127.0.0.1:9001/health/ready"
    "LLM Gateway"     = "http://127.0.0.1:9000/actuator/health"
    "Agent Runtime"   = "http://127.0.0.1:8001/api/v1/health/ready"
    "Context Service" = "http://127.0.0.1:8002/api/v1/health/ready"
    "RAG Query"       = "http://127.0.0.1:8003/api/v1/health/ready"
    "Tool Gateway"    = "http://127.0.0.1:9090/api/v1/health/ready"
    "Agent Web BFF"   = "http://127.0.0.1:9010/health/ready"
}
if ($WithIdentity) {
    $healthChecks["Keycloak"] = "http://127.0.0.1:9110/realms/agent-platform"
}

# 宿主端口只服务于浏览器、桌面端和本地验收；容器间始终使用 Compose 服务名及原始端口。
# 启动前实际尝试绑定可同时识别普通占用和 Windows excludedportrange，后者不会出现在
# Get-NetTCPConnection 中，却会导致 Docker 报“forbidden by its access permissions”。
$hostPorts = [ordered]@{
    "LLM Gateway"   = 9000
    "Governance"    = 9001
    "Control Plane" = 9002
    "Tool Gateway"  = 9090
    "Agent Runtime" = 8001
    "Context"       = 8002
    "RAG Query"     = 8003
    "Ingestion"     = 8004
    "Model Lab"     = 9091
    "Agent Lab"     = 9092
    "Agent Web BFF" = 9010
    "Keycloak"      = 9110
}

# Compose 首次构建会并行请求多个 Docker Hub token；部分 Windows 网络环境在 DNS、
# 代理或 BuildKit 冷启动时会让这些请求超时。先串行缓存公共基础镜像，可缩小故障面，
# 也让后续重试只重建业务层，而不会重复下载运行时。
$baseImages = @(
    "python:3.12-slim",
    "maven:3.9-eclipse-temurin-21",
    "eclipse-temurin:21-jre",
    "postgres:17-alpine",
    "redis:7-alpine",
    "quay.io/minio/minio:RELEASE.2025-09-07T16-13-09Z",
    "quay.io/minio/mc:RELEASE.2025-08-13T08-35-41Z"
)
if ($WithIdentity) { $baseImages += "quay.io/keycloak/keycloak:26.7.1" }

function Resolve-DockerCli {
    if (Get-Command docker -ErrorAction SilentlyContinue) { return }

    # Docker Desktop 安装后，旧 PowerShell/Codex 进程不会自动获得更新后的 PATH。
    $dockerBin = Join-Path $env:ProgramFiles "Docker\Docker\resources\bin"
    $dockerCliPlugins = Join-Path $env:ProgramFiles "Docker\cli-plugins"
    $dockerExe = Join-Path $dockerBin "docker.exe"
    if (Test-Path -LiteralPath $dockerExe) {
        $env:Path = "$dockerBin;$dockerCliPlugins;$env:Path"
    }
}

function Initialize-BaseImages {
    foreach ($image in $baseImages) {
        & docker image inspect $image *> $null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[CACHED] $image" -ForegroundColor DarkGreen
            continue
        }

        $pulled = $false
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            Write-Host "正在预取基础镜像 $image（第 $attempt/3 次）……" -ForegroundColor Cyan
            & docker pull $image
            if ($LASTEXITCODE -eq 0) {
                $pulled = $true
                break
            }
            if ($attempt -lt 3) { Start-Sleep -Seconds ([Math]::Pow(2, $attempt)) }
        }
        if (-not $pulled) {
            throw "基础镜像 $image 拉取失败。请检查 Docker Desktop 的代理/DNS 设置后重试。"
        }
    }
}

function Invoke-Compose {
    param([Parameter(Mandatory = $true)][string[]]$ComposeArguments)

    & docker compose --project-name $projectName @composeFileArguments @ComposeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose 执行失败，退出码：$LASTEXITCODE"
    }
}

function Wait-DockerEngine {
    Resolve-DockerCli
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
        $virtualization = if ($cpu.VirtualizationFirmwareEnabled) { "已开启" } else { "未开启" }
        throw @"
未找到 Docker CLI，七服务尚未启动。
检测结果：CPU=$($cpu.Name)；BIOS/UEFI 硬件虚拟化=$virtualization。

请按顺序完成：
1. 如果显示“未开启”，重启进入 BIOS，在 Advanced/CPU Configuration 中启用
   Intel (VMX) Virtualization Technology，然后保存退出。
2. 以管理员身份打开 PowerShell，执行：wsl --install --no-distribution
3. 重启 Windows。
4. 以管理员身份执行：
   winget install --id Docker.DockerDesktop --exact --accept-package-agreements --accept-source-agreements
5. 启动 Docker Desktop，等待 Engine Running，再重新双击 start-platform.cmd。
"@
    }
    & docker compose version *> $null
    if ($LASTEXITCODE -ne 0) {
        throw "当前 Docker 未包含 Compose V2。"
    }
    & docker info *> $null
    if ($LASTEXITCODE -eq 0) { return }

    $desktop = Join-Path $env:ProgramFiles "Docker\Docker\Docker Desktop.exe"
    if (-not (Test-Path -LiteralPath $desktop)) {
        throw "Docker Engine 未运行，且没有找到 Docker Desktop。"
    }
    Write-Host "Docker Engine 未运行，正在启动 Docker Desktop……" -ForegroundColor Yellow
    Start-Process -FilePath $desktop -WindowStyle Minimized | Out-Null
    $deadline = (Get-Date).AddSeconds([Math]::Min($TimeoutSeconds, 180))
    do {
        Start-Sleep -Seconds 3
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) { return }
    } while ((Get-Date) -lt $deadline)
    throw "等待 Docker Engine 启动超时。"
}

function Assert-HostPortsAvailable {
    # 已由本 Compose 项目运行的服务可以原地复用，不能把自身端口误判成外部冲突。
    $runningServices = @(& docker compose --project-name $projectName @composeFileArguments ps --status running --services)
    $serviceByName = @{
        "LLM Gateway" = "llm-gateway"; "Governance" = "agent-governance"
        "Control Plane" = "agent-control-plane"; "Tool Gateway" = "tool-gateway"
        "Agent Runtime" = "agent-runtime"; "Context" = "agent-context-service"
        "RAG Query" = "rag-query-api"; "Ingestion" = "ingestion-api"
        "Model Lab" = "model-lab"; "Agent Lab" = "agent-lab"; "Keycloak" = "keycloak"
    }
    foreach ($entry in $hostPorts.GetEnumerator()) {
        $composeService = $serviceByName[$entry.Key]
        if ($managedServices -notcontains $composeService) { continue }
        if ($runningServices -contains $composeService) { continue }
        # Docker down 刚结束时，Windows 可能短暂保留 docker-proxy 的监听句柄；这里有限
        # 重试而不是把正常的端口释放竞态误报为永久冲突。
        $available = $false
        for ($attempt = 1; $attempt -le 5; $attempt++) {
            $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Any, [int]$entry.Value)
            try {
                $listener.Start()
                $available = $true
                break
            }
            catch {
                if ($attempt -lt 5) { Start-Sleep -Seconds 1 }
            }
            finally {
                try { $listener.Stop() } catch { }
            }
        }
        if (-not $available) {
            throw "宿主端口 $($entry.Value)（$($entry.Key)）持续不可用。它可能被进程占用或被 Windows 保留；请运行 netsh interface ipv4 show excludedportrange protocol=tcp 查看。"
        }
    }
}

function Wait-ServiceHealth {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Uri
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)
    do {
        try {
            $response = Invoke-WebRequest -Uri $Uri -UseBasicParsing -TimeoutSec 5
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                Write-Host "[UP] $Name  $Uri" -ForegroundColor Green
                return
            }
        }
        catch {
            # 构建完成不等于依赖就绪；在统一截止时间内只重试网络和 5xx 启动窗口。
        }
        Start-Sleep -Seconds 2
    } while ((Get-Date) -lt $deadline)
    throw "$Name 未在 $TimeoutSeconds 秒内就绪：$Uri"
}

function Find-PythonRuntime {
    $workspacePython = Join-Path $repositoryRoot "agent-runtime\.venv312\Scripts\python.exe"
    if (Test-Path -LiteralPath $workspacePython) {
        return [PSCustomObject]@{ Command = $workspacePython; Prefix = @() }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        return [PSCustomObject]@{ Command = "py"; Prefix = @("-3.12") }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        return [PSCustomObject]@{ Command = "python"; Prefix = @() }
    }
    return $null
}

function Invoke-PlatformE2E {
    if ($SkipE2E) {
        Write-Host "已按参数跳过跨服务 E2E。" -ForegroundColor Yellow
        return
    }
    $python = Find-PythonRuntime
    if ($null -eq $python) {
        Write-Warning "未找到 Python 3.12，服务已启动，但跳过 scripts/platform_e2e.py。"
        return
    }
    & $python.Command @($python.Prefix) -c "import httpx" *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "Python 环境未安装 httpx，服务已启动，但跳过跨服务 E2E。"
        return
    }
    Write-Host "正在执行发布、Runtime、Tool 与 Governance 黑盒联调……" -ForegroundColor Cyan
    & $python.Command @($python.Prefix) (Join-Path $repositoryRoot "scripts\platform_e2e.py")
    if ($LASTEXITCODE -ne 0) {
        throw "平台 E2E 未通过。"
    }
}

function Warn-MissingModelCredential {
    # Gateway 可在没有上游模型凭证时正常启动，便于离线检索/工具联调；但真实模型决策
    # 会被上游明确拒绝。启动时提前说明，避免把后续 401/配置错误误判为 Runtime 故障。
    $environmentFile = Join-Path $repositoryRoot ".env"
    if (-not (Test-Path -LiteralPath $environmentFile)) {
        Write-Warning "未找到 .env：平台可启动，但真实 DeepSeek 调用不可用。复制 .env.example 为 .env 并设置 DEEPSEEK_API_KEY 后重启。"
    }
}

function Show-Endpoints {
    Write-Host ""
    Write-Host "七服务已就绪：" -ForegroundColor Cyan
    foreach ($entry in $healthChecks.GetEnumerator()) {
        Write-Host ("  {0,-17} {1}" -f $entry.Key, $entry.Value)
    }
    Write-Host "  Control Plane API http://127.0.0.1:9002/docs"
    Write-Host "  Runtime API       http://127.0.0.1:8001/docs"
    Write-Host "  Agent Web BFF     http://127.0.0.1:9010/health/ready"
    if ($WithIdentity) {
        Write-Host "  Keycloak          http://127.0.0.1:9110（本地 OIDC 管理入口）"
    }
    Write-Host ""
    Write-Host "桌面端连接地址：http://127.0.0.1:8001/api/v1（默认 demo/general-agent/local 已发布）" -ForegroundColor Green
}

Push-Location $repositoryRoot
try {
    Wait-DockerEngine
    switch ($Action) {
        "Stop" {
            # down 不携带 -v：容器与网络可重建，数据库卷和实验数据继续保留。
            Invoke-Compose -ComposeArguments @("down", "--remove-orphans")
            Write-Host "平台已停止，持久化卷未删除。" -ForegroundColor Green
            return
        }
        "Status" {
            Invoke-Compose -ComposeArguments @("ps")
            return
        }
        "Logs" {
            Invoke-Compose -ComposeArguments (@("logs", "--tail", "200", "--follow") + $managedServices)
            return
        }
        "Restart" {
            Invoke-Compose -ComposeArguments @("down", "--remove-orphans")
        }
    }

    Assert-HostPortsAvailable
    Warn-MissingModelCredential
    Initialize-BaseImages

    $upArguments = @("up", "--detach")
    if (-not $NoBuild) { $upArguments += "--build" }
    $upArguments += $managedServices
    Write-Host "正在启动七服务及运行依赖……" -ForegroundColor Cyan
    Invoke-Compose -ComposeArguments $upArguments

    try {
        foreach ($entry in $healthChecks.GetEnumerator()) {
            Wait-ServiceHealth -Name $entry.Key -Uri $entry.Value
        }
        Invoke-PlatformE2E
        Show-Endpoints
    }
    catch {
        Write-Host "启动验收失败，输出最近容器日志：" -ForegroundColor Red
        & docker compose --project-name $projectName @composeFileArguments logs --tail 120 @managedServices
        throw
    }
}
finally {
    Pop-Location
}
