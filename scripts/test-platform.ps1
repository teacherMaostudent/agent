<#
本地统一回归入口。各 Python 服务使用自己的测试配置与工作目录，避免多个 app 包互相遮蔽。
默认执行现有测试、类型检查、前端构建、静态生产检查及 Compose 配置校验，不启动或停止平台。
WithIntegration 会在运行中的本地服务创建测试发布、任务、文档和保留对象，不适用于生产环境。
WithOidc 只从环境变量 AUDIT_USERNAME/AUDIT_PASSWORD 读取账号，绝不在参数或日志中回显凭证。
#>
[CmdletBinding()]
param(
    [switch]$WithIntegration,
    [switch]$WithOidc
)

$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
$checks = [System.Collections.Generic.List[object]]::new()

function Invoke-Check {
    param([string]$Name, [string]$Directory, [string]$Command, [string[]]$Arguments)
    # 每条命令立即捕获退出码；不能让后一条成功命令掩盖前一条失败。
    Push-Location -LiteralPath (Join-Path $repoRoot $Directory)
    $code = 1
    try {
        Write-Host "`nCHECK: $Name"
        & $Command @Arguments
        $code = $LASTEXITCODE
    } catch {
        Write-Warning "$Name could not run: $($_.Exception.Message)"
    } finally {
        Pop-Location
        $checks.Add([pscustomobject]@{ Check = $Name; ExitCode = $code })
    }
}

foreach ($project in @('agent-runtime','rag-agent-service','agent-control-plane','agent-governance',
        'tool-gateway','agent-web-bff','agent-lab','model-lab','platform-infra')) {
    Invoke-Check $project $project py @('-3.12','-m','pytest','-q')
}
Invoke-Check 'LLM Gateway' 'llm-gateway' mvn @('-B','test')
Invoke-Check 'Desktop tests' 'agent-desktop' pnpm @('test')
Invoke-Check 'Desktop types' 'agent-desktop' pnpm @('typecheck')
Invoke-Check 'Desktop build' 'agent-desktop' pnpm @('run','build')
Invoke-Check 'Web tests' 'agent-web' pnpm @('test')
Invoke-Check 'Gateway Console build' 'llm-gateway/frontend' pnpm @('run','build')
Invoke-Check 'Python lint' '.' py @('-3.12','-m','ruff','check','agent-runtime','rag-agent-service',
    'agent-control-plane','agent-governance','tool-gateway','agent-web-bff','agent-lab','model-lab',
    'platform-infra','platform-sdk','scripts/platform_e2e.py','scripts/local_scenarios.py',
    'scripts/web_session_e2e.py')
Invoke-Check 'Production static check' '.' py @('-3.12','scripts/production_readiness.py')
Invoke-Check 'Local Compose schema' '.' docker @('compose','-f','compose.platform.yaml',
    '-f','compose.identity.yaml','-f','compose.audit-dev.yaml','config','--quiet')
Invoke-Check 'Production Compose schema' '.' docker @('compose','--env-file','.env.production.example',
    '-f','compose.production.yaml','config','--quiet')

if ($WithIntegration) {
    Invoke-Check 'Release-runtime-tool-audit E2E' '.' py @('-3.12','scripts/platform_e2e.py')
    Invoke-Check 'Local service scenarios' '.' py @('-3.12','scripts/local_scenarios.py')
}
if ($WithOidc) {
    if (-not $env:AUDIT_USERNAME -or -not $env:AUDIT_PASSWORD) {
        Write-Warning 'WithOidc requires AUDIT_USERNAME and AUDIT_PASSWORD; check is FAILED, not skipped.'
        $checks.Add([pscustomobject]@{ Check = 'OIDC session'; ExitCode = 1 })
    } else {
        Invoke-Check 'OIDC session and task' '.' py @('-3.12','scripts/web_session_e2e.py','--submit-task')
    }
}

# 无独立 SDK 测试不能被统计成通过；其当前覆盖来自各服务间接测试。
Write-Warning 'platform-sdk has no standalone test suite. Browser/native UI and production HA are not covered by this runner.'
$checks | Format-Table -AutoSize
if (@($checks | Where-Object ExitCode -ne 0).Count) { exit 1 }
exit 0
