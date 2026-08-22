# 统一 Agent Web 产品设计与现状审计

> 状态：2026-08-22 代码闭环版。本文只把已有实现与已运行测试证明的能力标为完成；真实 IdP、KMS、S3、Kafka 与多节点环境仍需部署验收。

## 1. 决策

平台应建设一个统一的 Web 产品，而不是分别建设三个相互割裂的前端。统一登录、租户切换、通知、任务链接和审计关联降低协作成本；`Workspace`、`Review`、`Console` 则以独立路由、独立 API 范围和服务端授权隔离。前端隐藏菜单只能改善体验，绝不能成为权限边界。

`agent-desktop` 不再定位为平台主界面。它演进为可选的 **Desktop Connector**：在用户明确授权后提供本地文件选择、受控扫描、IDE/终端集成、原生通知和断线辅助。它不能修改发布、路由、预算、审计或工具目录，也不能把本机目录选择升级为服务端工具权限。

```text
                 OIDC 登录 / Tenant / Policy Decision
                                  │
                             Agent Web
          ┌───────────────────────┼────────────────────────┐
          ▼                       ▼                        ▼
     /workspace               /review                 /console
     业务用户                  专家/主管                平台/运维人员
          │                       │                        │
          └────────────── Runtime BFF / Policy API ────────┘
                                  │
      Runtime · Context · RAG · LLM Gateway · Tool Gateway · Governance · Control Plane
                                  │
                    可选 Desktop Connector（本机能力）
```

## 2. 三个工作区

### 2.1 Agent Workspace：完成业务目标

目标用户是业务使用者。首页只展示“新建任务、进行中任务、待我确认、已完成交付物”，而非 Run ID、OIDC Token、模型路由或原始事件。用户输入目标、选择已授权 Agent、附加平台已允许的资料，并得到结论、可下载工件、引用与下一步建议。

任务详情采用四段式：**目标**（用户表达与约束）、**进度**（自然语言阶段和可中断状态）、**结果**（答案、工件、引用、失败原因）、**需要你决定**（仅当审批/澄清/冲突发生）。默认只展示证据摘要和风险说明；“查看执行依据”才展示计划摘要、选用工具、模型路由和成本摘要。业务用户只能读取自己或被共享的任务。

本机文件操作从 Workspace 发起，但浏览器不会获得磁盘权限。Web 发出带 `run_id`、`agent_id`、`snapshot_id`、允许 capability、有效期与用户身份绑定的 Connector 请求；Desktop Connector 只显示用户可确认的目录/动作，受控执行后将脱敏、截断的结果提交给 Tool Gateway。Connector 断开时任务应进入明确的“等待本机连接”状态，而不是隐式改为容器扫描。

### 2.2 Agent Review：审查、协作与人工接管

目标用户是领域专家、主管和审批人。它以队列组织需要处理的运行：高风险、低置信、证据不足、模型/规则分歧、长时间卡住和人工转交。单个任务以“结论—证据—计划—风险—决定”为主线，允许查看完整 Evidence ID、检索失败/降级事实、计划节点、工具准入、成本账本和关联审计事件。

Review 的核心动作是澄清、批准、拒绝、补充 Steering、取消、转交、标注与复盘。审批必须绑定既有 `approval_id`、原始参数哈希、租户、有效期和一次性消费规则；Review 不得直接调用工具绕过 Runtime/Tool Gateway。专家标签应写入 Governance/Agent Lab 的受控反馈入口，而不是只保存在前端笔记。

### 2.3 Platform Console：治理与运维

目标用户是平台管理员、发布管理员、审计员和 SRE。它负责 Agent Draft/Spec/Release、Runtime 集群能力、工具目录版本、模型路由、评测门禁、成本与配额、审计检索、事件投影、故障与重放状态。Console 不承担业务对话入口，也不能借“管理员页面”跳过发布快照、质量门禁或审批。

高危操作（发布、Promote、Rollback、路由变更、配额变更、审计导出、DLQ 重放）必须有单独 permission、二次确认、请求理由、短时权限提升/强认证以及不可变审计。显示 API Key 时必须脱敏；供应商 Key、私钥和原始敏感 Prompt 不进入浏览器。

## 3. 路由与授权模型

建议的首批路由如下。路由守卫仅决定是否渲染；每个数据接口仍须由后端基于 JWT、Tenant 与资源关系重做授权。

| 路由 | 最小角色/权限 | 后端资源范围 | 禁止事项 |
| --- | --- | --- | --- |
| `/workspace` | `agent.user` | 自己创建、被共享或被委派的 Run/Artifact | 跨用户检索、直接工具调用、读取发布草稿 |
| `/workspace/runs/:id` | `run.read` + Run 所有权/共享关系 | 单一 Run、公开级证据、自己的控制输入 | 读取完整审计、跨租户 ID 枚举 |
| `/review` | `agent:review` + 显式 Review Assignment | 当前审查人被指派的队列 | 读取未授权业务域、绕过 approval_id |
| `/review/runs/:id` | `agent:review` + 单 Run Assignment | 受控计划摘要、Evidence ID、预算与审批状态 | 修改历史事实、直接提交工具副作用 |
| `/console/services` | `ops:read` | 七服务边界及摄取工作负载的无敏感就绪状态 | 使用一个泛化 `admin` 角色覆盖一切 |
| `/console/*`（后续高危动作） | 分拆的 `release.*`、`model.*`、`audit.*`、`ops.*` | 指定 Tenant/环境/资源类型 | 使用一个泛化 `admin` 角色覆盖一切 |

请求身份只接受 OIDC 验证后的 subject、tenant、roles、permissions、数据域和认证强度。前端绝不自行写入 `X-Tenant-Id`、`X-Roles`、`X-Permissions` 作为信任依据；开发环境 Header 兼容层不能暴露给公网 Web。

## 4. 前端与 API 形态

新增 `agent-web` 前端（建议 React + TypeScript + Vite；与现有 Electron Renderer 可共享纯展示组件和 OpenAPI/契约类型，但不共享 Electron 主进程代码）。在其前设置 BFF/API Gateway：浏览器只面对同源 BFF，BFF 交换/验证 OIDC Session、施加 CSRF 与请求限流、按页面拼装最小投影，再用工作负载身份调用七服务。

不能让浏览器直接拼接七个服务地址，原因包括 CORS 扩张、令牌泄露面、跨服务分页语义不一致和每个页面重复授权。BFF 不是第二个业务状态机：它不得编排 Agent、保存 Release 或裁定工具权限；它仅做身份会话、资源聚合、投影和 Web 专用协议适配。

首批 BFF 投影建议：

| Web 投影 | 来源 | 最小字段 |
| --- | --- | --- |
| 我的任务 / 任务详情 | Runtime | 状态、自然语言阶段、结果、工件引用、允许控制动作 |
| 证据卡片 | Runtime + RAG | evidence_id、来源、片段、置信/不足声明、权限投影 |
| Review 队列 | Runtime + Governance + Tool Gateway | 原因、风险、截止时间、审批状态、责任人 |
| 发布中心 | Control Plane | Draft/Spec/Release、质量门禁、目标 Runtime 能力 |
| 模型与成本 | LLM Gateway + Governance | 路由版本、健康、聚合成本、限流/熔断事实 |
| 审计与运维 | Governance | 脱敏事件、Trace 关联、投影延迟、DLQ/重放状态 |

## 5. Desktop Connector 契约

Connector 的权限低于平台控制面，但拥有被用户显式授权的本机执行能力。其最小协议应包含：

1. Web 通过已登录会话请求 Connector 配对，不把 OIDC 长期 Token 交给 Renderer；
2. Connector 在 OS 层提示用户选择目录/动作，并发送目录指纹、清单和范围声明；
3. Runtime/Tool Gateway 验证 Release Tool Binding、scope、参数、预算和审批后签发短期一次性执行授权；Tool Gateway 以 opaque grant 哈希持久化，并在工具名、版本、Run、Snapshot 任一不匹配或重复消费时拒绝；
4. Connector 执行白名单能力，限制路径、文件类型、输出大小、超时和网络访问；
5. 结果先脱敏、标记来源和哈希，再经 Tool Gateway 回写为关联 `run_id` 的事实；
6. Connector 只持有短期设备会话，不保存供应商 Key、控制面管理员 Key 或可复用审批令牌。

## 6. 现状审计（2026-08-22）

| 范围 | 已有基础 | 缺口 / 风险 | 判定 |
| --- | --- | --- | --- |
| Workspace 运行 | Runtime 提供 Run、事件流、取消、Steering、审批恢复、结果与 Context 摘要；Web 只列 owner 或明确共享任务。Artifact 下载先校验资源关系再签发 5 分钟 URL；文本预览受媒体白名单、对象前缀和内容上限约束，同一 `logical_name` 使用不可变版本链进行 unified diff。SSE 支持按游标重连。 | 二进制 Office/PDF 的服务端渲染预览仍应由独立隔离转换服务承担，当前不会把它们误当文本解码。 | 文本工件闭环完成 |
| Review | 队列与详情同时校验 `agent:review` 和单 Run Assignment；批准、拒绝、转交、评论、共同审查人、专家标签分别使用独立权限。证据正文还需 `evidence:content:read`，若证据声明数据域则继续要求 `data-domain:{domain}:read`，正文脱敏来源沿用 Runtime 结果并限制 12,000 字符。共同审查新增 Assignment，转交才移除当前 Assignment。 | Golden Candidate 与原始线上样本的并排差异界面仍可增强；Governance 归属与授权链已存在。 | 核心协作闭环完成 |
| Console | `ops:read` 聚合服务健康；`release:read` 浏览 Draft/Release。Draft 校验、版本冻结、Release 创建、Promote、Pause、Rollback、模型路由 Release、租户/用户 Token/成本配额、Connector DLQ 和 WORM 导出作业均由 BFF 聚合，后端仍各自拥有状态机。高风险写操作要求独立 permission、目标 ID 回显、原因和生产近期 MFA/ACR。 | 多管理员并发策略编辑仍需依赖 Control Plane 的更新冲突处理和运维变更窗口；供应商 Key 永不进入 Web。 | Console 关键动作闭环 |
| 身份边界 | Web BFF 已实现 Authorization Code + PKCE、state/nonce、ID Token 验签、Redis 服务端会话、HttpOnly Cookie、退出撤销、同源 CSRF、严格 CSP 哈希和独立 mTLS 客户端身份。浏览器不保存访问令牌。 | 必须由部署方提供真实 IdP Client、回调地址、证书和 Redis；未提供前只能验证代码与配置 fail-closed，不能声称完成真实 SSO 联调。 | 代码闭环，外部部署待验收 |
| Run 所有权 | Workspace 按 `tenant + owner` 或显式 Share 读取，控制动作只属于 owner；Review 使用独立 Assignment，转交和共同审查均通过原子关系变更，不能传任意 user/reviewer 扩大查询范围。 | 企业组织架构的“部门主管自动继承可见范围”没有隐式实现；如需该能力必须由 Control Plane 发布明确的数据域/组织策略后再生成 Assignment，不能按职位字符串猜测。 | 显式关系闭环完成 |
| 证据 | Workspace 默认只收到 Evidence ID、来源和引用，不返回原始正文；Review 正文读取同时校验 Assignment、`evidence:content:read` 和数据域权限，并进行长度限制与摘要校验。 | 高敏原文仍应由数据源/对象存储执行字段级脱敏和保留策略；Runtime 投影不能替代源系统 DLP。 | 角色投影闭环完成 |
| Desktop | 配对、心跳/断线、Run/Snapshot/工具版本绑定、一次性 Grant、人工确认、本机受控扫描、脱敏/限长/哈希、Tool Gateway 幂等审计回执和 Context Artifact 交付均已接线。`controlled_scan` 交付后生成独立知识晋升请求；有权限的 owner/reviewer 审批后，摄取 Relay 以稳定 ID 投递 RAG 耐久任务。 | `SUBMITTED` 只表示摄取服务已接收，索引完成/失败以摄取 Job 为事实源；Desktop 永远不能绕过审批直接写索引。 | 执行、交付与审批摄取闭环 |
| 本地扫描 | 服务端 `controlled_scan` 仍支持容器白名单 Scope；Desktop Connector 另由用户显式选择本机目录，在主进程中限制深度、文件数、单文件大小、结果数、敏感信息和输出长度，并使用一次性 Grant/收据回写。 | 当前只支持字面量受控文本扫描，不是任意 Shell、IDE 或自动改写器；二进制解析、Git Patch 审批和隔离代码执行属于独立能力。 | 本机只读扫描闭环完成 |
| 安全运维 | 生产 BFF 强制 OIDC/PKCE/Redis/mTLS；高风险动作要求近期 MFA/ACR、独立 permission 和目标确认。Console 可创建、查询和重排 Governance WORM 作业，但签名和 Object Lock 写入只在独立 Worker 中执行。本地 Keycloak Overlay、MinIO Object Lock、Prometheus/Alertmanager/Blackbox 基线与 Temporal Global Namespace 演练脚本均已提供。 | 企业 IdP 租户、正式证书/KMS/对象桶、告警接收器及真实双区域集群属于目标环境配置与验收，仓库不能替代它们。 | 代码与部署模板闭环，环境待验收 |

审计结论：统一 Web 的 Workspace、Review、Console 与可选 Desktop Connector 已形成代码闭环；Artifact 文本预览/版本对比、模型路由与配额管理、WORM 作业入口、扫描结果审批摄取均已落到服务端授权和耐久状态，而不是只增加页面按钮。仍不能把“代码闭环”表述成“生产环境验收完成”：企业 IdP、正式 mTLS CA/叶证书、Redis/PostgreSQL HA、KMS/WORM 对象桶、真实告警接收器和双区域 Temporal 集群必须在目标环境完成联调和演练。

## 7. 实施顺序与验收

1. **身份与资源关系（已完成代码验收）**：Web subject、Run owner/share/review assignment、数据域权限与 action permission 均在服务端校验；契约测试覆盖越权拒绝。
2. **Web 壳与 Workspace（已完成核心闭环）**：PKCE、Redis Session、Workspace、SSE 重连、受控工件下载、取消/Steering/审批已接线；真实 IdP 与对象存储作为部署验收项。
3. **Review（已完成核心闭环）**：显式队列、计划摘要、单/多人 Assignment、转交、评论、证据数据域授权、审批和专家标签回流已接线。
4. **Console（关键管理闭环）**：服务健康、目录读取、版本/Release 状态动作、模型路由、配额、Golden 审核、Connector DLQ 和 WORM 导出已接线。
5. **Desktop Connector（执行、交付与知识晋升闭环）**：短期配对、心跳、一次性 Grant、人工确认、白名单扫描、幂等审计、Artifact Relay/DLQ 及审批后 RAG 摄取已接线；生产环境使用独立身份和网络策略。

## 8. 不做的事情

- 不在前端复刻 Planner、Harness、Graph 或工具授权；
- 不将 `Run ID`、原始审计事件、OIDC Token、供应商 Key 默认暴露给业务用户；
- 不用一个“管理员”布尔值替代资源级授权；
- 不让 Desktop Connector 成为绕过 Web/Runtime/Tool Gateway 的高权限代理；
- 不以“页面隐藏”替代 API 端的 Tenant、数据域、所有权和审批校验。
