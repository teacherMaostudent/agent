# 从本项目学习 Python：面向生产级 Agent 平台的语法、设计与架构教程

这不是一份孤立的 Python 语法清单，而是从本仓库的 Agent Runtime、RAG、Context、Tool Gateway、Control Plane、Governance、Model Lab 和平台 SDK 中反推出来的学习路径。目标是理解：同一个 Python 特性为什么会出现在不同服务中，它解决什么工程问题，以及什么时候不该使用它。

阅读代码时，建议保持一条主线：**HTTP 请求进入 API 层，经过应用服务编排，依赖领域模型表达规则，再由基础设施层访问数据库、消息队列或外部服务。** Python 语法服务于这条主线，而不是目的本身。

## 0. Python 基础语法：先能读懂每一行

Python 用缩进表示代码块，不用花括号；同一代码块必须保持相同缩进。变量不需要预先声明类型，但在工程代码中应通过类型标注说明意图。

```python
retry_count: int = 0
max_retries: int = 2
request_id = "req_123"             # 字符串
remaining_budget = Decimal("1.20") # 金额不要用 float

if retry_count < max_retries:
    retry_count += 1
else:
    raise RuntimeError(f"请求 {request_id} 已达到重试上限")
```

`if` 的条件通常来自状态、权限或预算，而不是仅仅演示真假。比较字符串时使用 `==`，判断对象身份才使用 `is`；判断空值使用 `value is None`，不要写 `value == None`。`f"...{name}..."` 是推荐的格式化字符串写法。

循环用于处理同类对象，迭代变量应使用有业务含义的名字：

```python
for evidence in knowledge_evidence:
    if evidence.score < min_score:
        continue
    selected.append(evidence)
```

`continue` 跳过当前项，`break` 结束循环，`return` 结束整个函数。若循环中既有权限过滤、排序、预算扣减又有审计记录，宁可分成数个清晰步骤，也不要压缩成难以审查的一行代码。

Python 的 `None` 表示“没有值”，但业务上必须进一步说明：是“没有检索结果”、 “可选字段未填写”还是“上游调用失败”？生产 API 不应把这三种情形都混为 `None`。

## 1. 先建立工程地图：Python 包、模块与职责

Python 的一个 `.py` 文件是模块；带有 `__init__.py` 的目录通常是包。项目不应按“语法类别”组织，而应按职责组织。例如：

```text
tool-gateway/app/
├── api/                 # HTTP 路由与身份提取
├── application.py       # 工具调用用例编排
├── domain/              # 请求、状态、错误和业务规则
├── infrastructure/      # HTTP/MCP 适配器、数据库、网络安全
└── container.py         # 依赖装配与生命周期管理
```

这对应分层架构：`api` 不直接执行 SQL；`domain` 不应依赖 FastAPI；`infrastructure` 可以替换；`application` 负责把它们串起来。你可以从 [Tool Gateway application.py](../tool-gateway/app/application.py) 开始，再顺着 `Container` 找到其依赖。

### 导入不是“把所有东西拿进来”

```python
from app.domain.models import InvocationRequest
from app.infrastructure.adapters import ToolAdapter
```

第一行依赖稳定的业务契约，第二行依赖可替换的执行抽象。相反，若 Runtime 直接 `import rag-agent-service` 的内部仓储实现，就会把两个服务绑成一个部署单元。这正是本工程抽出 `platform-sdk` 的原因：跨服务只能共享契约和客户端，不能共享对方的应用层实现。

## 2. 函数：用签名表达输入、输出与失败方式

生产代码中函数不是“执行一段语句”，而是一份小契约。

```python
def redact_text(value: str) -> str:
    """脱敏可进入日志或审计的文本，避免原文继续传播。"""
    ...
```

这个签名同时表达三件事：输入必须是字符串、返回仍是字符串、函数的安全边界是日志/审计输出。真实实现可见 [platform-sdk 的 redaction.py](../platform-sdk/platform_sdk/security/redaction.py)。

### 2.1 位置参数、关键字参数与默认值

```python
def bound_untrusted(value: object, *, max_chars: int = 12_000) -> object:
    ...

bound_untrusted(content, max_chars=4_000)  # 正确：限制值必须写出名字
```

`*` 后的参数只能以关键字传递。对于预算、超时、权限、租户等容易被误传的参数，这比 `bound_untrusted(content, 4000)` 更安全、更易审查。默认值适合稳定策略；不要把可变对象作为默认值：

```python
# 错误：所有调用共享同一个列表
def add_evidence(item, cache=[]):
    cache.append(item)

# 正确：每次调用各自创建列表
def add_evidence(item, cache: list[str] | None = None):
    cache = [] if cache is None else cache
    cache.append(item)
```

### 2.2 返回值：优先返回结构化结果，而不是隐式约定

不要让调用方猜测 `None`、空字典和异常分别意味着什么。检索、工具调用、审批等跨边界操作，应该返回 Pydantic 模型或明确的领域对象：

```python
class SearchResult(BaseModel):
    evidence: list[Evidence]
    degraded_to_memory: bool = False
    reason: str | None = None
```

这样 Runtime 可以区分“没有证据”“RAG 不可用但已 memory-only 降级”“调用失败”。结构化降级是可观测的业务状态，不是吞掉异常。

### 2.3 Docstring 是接口文档，不是重复函数名

```python
def reserve_budget(run_id: str, amount: Decimal) -> Reservation:
    """原子预留本次运行的最大成本，拒绝超过已发布预算的请求。"""
```

好的说明交代约束、状态变化或安全含义；`"""预留预算。"""` 虽不算错，却无法帮助读者理解并发和拒绝规则。本项目的全量注释采用这一标准。

## 3. 类型标注：让 Python 的动态性可被检查

Python 在运行时仍是动态语言，但类型标注让 IDE、Ruff、Pyright/mypy 和读代码的人提前发现接口不一致。

```python
from typing import Any

def summarize(evidence: list[Evidence]) -> dict[str, Any]:
    return {
        "count": len(evidence),
        "document_ids": [item.document_id for item in evidence],
    }
```

常用形式：

| 写法 | 含义 | 本工程中的典型用途 |
| --- | --- | --- |
| `str | None` | 字符串或空值 | 可选 trace、失败原因 |
| `list[Evidence]` | 同类对象列表 | RAG 证据集合 |
| `dict[str, Any]` | 键固定、值开放 | 外部 JSON Payload |
| `Literal["draft", "released"]` | 受限字符串集合 | 发布或执行状态 |
| `Annotated[T, ...]` | 类型附加框架元数据 | FastAPI 路由依赖 |

`Any` 是“放弃静态检查”的逃生舱，通常仅用于外部 JSON、插件参数或兼容层；进入领域逻辑后应尽快转换成明确模型。

## 4. Pydantic：把不可信 JSON 变成可信领域数据

FastAPI 接收的是网络 JSON，不能直接当作业务对象使用。Pydantic `BaseModel` 会完成解析、默认值、类型检查和序列化。本项目的请求、发布快照、上下文包、审计事件都用这种方式表达。

```python
from pydantic import BaseModel, ConfigDict, Field, field_validator

class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=128)
    timeout_seconds: int = Field(default=30, ge=1, le=120)

    @field_validator("tool_name")
    @classmethod
    def reject_blank_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("tool_name 不能为空")
        return value
```

这里的设计要点：

- `extra="forbid"`：拒绝拼错字段和未审查字段，适合安全边界；
- `Field`：把长度、范围等局部不变量紧贴字段；
- `field_validator`：处理单字段规范化；
- `model_validator`：处理“多个字段必须同时满足”的规则，例如 HTTP 与 MCP 传输配置不能同时存在。

可对照 [Tool Gateway domain/models.py](../tool-gateway/app/domain/models.py) 与 [Governance domain/models.py](../agent-governance/app/domain/models.py)。不要用 Pydantic 替代所有业务逻辑：它负责输入契约，跨聚合的一致性、数据库 CAS、审批状态机仍应在应用服务和仓储层实现。

## 5. `dataclass`、`Enum` 与不可变值对象

### 5.1 `dataclass`：轻量内部值对象

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ToolContext:
    tenant_id: str
    run_id: str
    trace_id: str
```

`frozen=True` 让实例不可修改，适合权限上下文、审计记录、路由选择结果：一个对象一旦被用于鉴权或记账，就不应在流程中途被悄悄改写。可对照 [platform-sdk 工具注册表](../platform-sdk/platform_sdk/tools/registry.py)。对外 API 的 JSON 则优先用 Pydantic，因为它具备验证与 OpenAPI 能力。

### 5.2 `StrEnum`：让状态有限且可序列化

```python
from enum import StrEnum

class InvocationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
```

与任意字符串相比，枚举让非法状态更难出现，也让前端、数据库和事件 Payload 使用同一套字面量。注意：枚举只定义合法集合，不能单独保证状态迁移合法；`PENDING -> SUCCEEDED` 是否允许，应由状态机方法显式检查。

## 6. 类、封装与依赖注入：谁负责创建对象

```python
class ToolExecutionService:
    def __init__(self, repository: Repository, adapter: ToolAdapter) -> None:
        self._repository = repository
        self._adapter = adapter

    async def invoke(self, request: InvocationRequest) -> InvocationResponse:
        ...
```

`__init__` 接收依赖，而不是在业务方法里 `Repository()`、`httpx.Client()`。这样测试可传入内存仓储或假适配器，生产可传 PostgreSQL、HTTP 或 MCP 实现。对象的实际装配集中在 `Container` 中，例如 [Tool Gateway container.py](../tool-gateway/app/container.py)。

这种模式称为**依赖注入**。它不是框架魔法，核心只是一条规则：**使用者声明自己需要什么，组合根决定给它什么实现。**

## 7. `Protocol`：面向能力编程，而非面向具体类编程

```python
from typing import Protocol

class ToolAdapter(Protocol):
    async def invoke(self, context: ToolContext, arguments: dict[str, object]) -> dict[str, object]:
        ...
```

任何拥有兼容 `invoke` 方法的对象都可被视为 `ToolAdapter`，不必继承共同父类。HTTP、MCP、受控扫描适配器因此可交换。真实例子位于 [adapters.py](../tool-gateway/app/infrastructure/adapters.py)。

`Protocol` 的价值在于依赖倒置：应用服务依赖“能调用工具”这一能力，而非依赖 `HttpToolAdapter`。这使新增工具传输方式不需要改核心编排代码。

## 8. 异步编程：`async`/`await` 不等于多线程

Agent 平台的大量时间花在等待：调用模型、检索、工具、Kafka、数据库。`async def` 允许一个协程等待网络时，让事件循环处理别的请求。

```python
async def invoke_tool(request: InvocationRequest) -> InvocationResponse:
    approval = await approval_service.ensure_approved(request)
    result = await adapter.invoke(approval.context, request.arguments)
    return result
```

三个要点：

1. 调用异步函数必须 `await`，否则只得到协程对象；
2. `async` 内部不能直接执行长时间 CPU 运算或阻塞 I/O；需要线程池、任务队列或独立 Worker；
3. 同一资源的并发安全仍需事务、锁、唯一约束或 CAS，`async` 不会自动解决竞争条件。

FastAPI 路由、Runtime Worker、Kafka 消费均使用这一模式。CPU 密集的文档解析或 OCR 则应通过摄取 Worker/Temporal 执行，避免堵住 Web 请求。

## 9. 异常：按边界分类，而不是 `except Exception: pass`

领域异常把失败转化成可处理的业务语义：

```python
class ToolNotFoundError(GatewayError):
    """请求的工具不在已发布目录中。"""

class ApprovalError(GatewayError):
    """高风险工具未完成一次性审批。"""
```

API 层把它们映射为一致的 HTTP 错误；应用层决定是否重试、降级或终止；基础设施层把网络库、数据库库的原始异常翻译出来。可对照 [Tool Gateway errors.py](../tool-gateway/app/domain/errors.py)。

```python
try:
    response = await adapter.invoke(...)
except TimeoutError as error:
    raise ToolTimeoutError("工具调用超时") from error
```

`raise ... from error` 保留根因链，利于 Trace 和排障。不要捕获所有异常后返回“成功但为空”的结果，这会让 Runtime 把系统故障误认为正常业务结果。

## 10. 集合、推导式与数据处理：简洁但要保持可读

RAG 的证据筛选经常使用列表推导式：

```python
allowed = [
    item
    for item in evidence
    if item.score >= threshold and item.document_id in permitted_document_ids
]
```

这比手写 `for` 循环更聚焦于“选择规则”。但当推导式包含多层 `if`、异常处理、指标记录或副作用时，应改成普通循环：安全、审计和指标逻辑需要可读的步骤，而不是一行聪明代码。

字典适合索引：`routes[route_id]`；集合适合去重与成员判断：`document_id in permitted_ids`；列表保留顺序，适合上下文消息与证据排序。根据数据语义选择容器，而不只是因为某种写法更短。

## 11. 数据一致性：Python 代码如何保护业务状态

语法并不能自动保证一致性。以“工具调用”举例，正确的状态流转大致是：

```text
校验目录与权限 → 创建幂等声明 → 检查/消费审批 → 预留预算
→ 调用适配器 → 写审计与 Outbox → 成功、失败或可重试
```

其中每一步都有相应 Python 设计：

- Pydantic 校验请求；
- `StrEnum` 表达调用与审批状态；
- 仓储方法在事务中进行唯一约束或 CAS；
- 自定义异常区分拒绝、超时和上游失败；
- Outbox 记录先与业务状态同事务落库，再由独立 Relay 发布事件。

不要在业务事务中直接发 Kafka：数据库提交失败而消息已经发布会产生幽灵事件。不要仅依靠 `if status == ...` 做并发控制：两个请求可能同时通过检查。实际仓储层应使用数据库条件更新、唯一键或乐观版本号。

## 12. FastAPI：路由只做协议适配

```python
@router.post("/tools/invoke")
async def invoke_tool(
    request: InvocationRequest,
    identity: Annotated[WorkloadIdentity, Depends(require_identity)],
    service: Annotated[ToolExecutionService, Depends(get_service)],
) -> InvocationResponse:
    return await service.invoke(request, identity)
```

路由函数应当：解析 HTTP、获取已验证身份、调用应用服务、返回模型。它不应包含 SQL、模型路由策略或工具协议实现。这样同一 `ToolExecutionService` 也可被异步 Worker 或测试直接调用。

这也是为什么身份不能直接信任 `X-User-Id` 一类 Header：中间件先验证 OIDC/JWT 或 mTLS，再创建受信身份对象；路由只接收已验证的结果。

## 13. 从一个需求走完设计：新增“受控文本扫描”工具

假设要增加一个可扫描日志/源码的工具，不能只写一个函数。建议按以下顺序实现：

1. 在领域模型定义 `ToolSpec`、风险等级、输入 Schema 和输出上限；
2. 在工具目录注册 `controlled_scan`，明确允许路径、正则限制、最大文件数与脱敏规则；
3. 为执行器实现 `ToolAdapter` 能力，拒绝越权路径和命令注入；
4. 在应用服务中复用鉴权、预算、审批、幂等、超时、审计与 Outbox；
5. 在 Control Plane 发布快照中绑定工具版本；
6. Runtime 只根据已发布快照决定能否调用，不直接绕过 Gateway；
7. 为成功、拒绝、超时、重复请求和审批过期写测试。

这说明 Python 函数只是最内层的“怎么扫”；平台架构解决的是“谁能扫、扫什么、何时扫、结果能否进入 Prompt、失败如何追溯”。

## 14. 阅读与练习路线

按下面顺序阅读，能把语法逐步连接到架构：

1. [platform-sdk/contracts](../platform-sdk/platform_sdk/contracts/)：Pydantic、类型、跨服务契约；
2. [tool-gateway/domain/models.py](../tool-gateway/app/domain/models.py)：枚举、校验器、领域模型；
3. [tool-gateway/infrastructure/adapters.py](../tool-gateway/app/infrastructure/adapters.py)：`Protocol`、异步 I/O、适配器；
4. [tool-gateway/application.py](../tool-gateway/app/application.py)：用例编排、审批、幂等、审计；
5. [agent-runtime 的 planner](../agent-runtime/src/agent_runtime_service/runtime/planner.py)：受控规划与预算；
6. [rag-agent-service 的 query_service](../rag-agent-service/app/rag/query_service.py)：检索、降级与证据；
7. [agent-governance 的 evaluation_service](../agent-governance/app/application/evaluation_service.py)：评测、质量门禁和校准。

每读完一个文件，尝试回答四个问题：输入在哪里被验证？状态在哪里落库？失败如何分类？该类依赖的是具体实现还是抽象能力？能回答这四个问题，说明你已经不只是会写 Python 语法，而是在理解生产 Agent 系统的 Python 设计。

## 15. 代码评审自查表

- 函数签名是否声明了输入、输出和可选参数的语义？
- 外部 Payload 是否在边界处转换为 Pydantic 模型？
- `async` 函数内是否混入阻塞 I/O 或长时间 CPU 任务？
- 应用服务是否依赖 `Protocol`/仓储抽象，而不是硬编码具体客户端？
- 异常是否保留根因，并在正确边界转为领域错误或 HTTP 响应？
- 是否通过数据库约束、事务、CAS 或幂等键处理并发，而非只靠内存判断？
- 日志、审计和 Prompt 前是否执行脱敏、截断与数据域检查？
- 新能力是否进入发布快照、权限目录、质量门禁和可观测链路？

如果这些问题大多有明确答案，代码通常已经具备从“能运行”走向“可维护、可审计、可扩展”的基础。
