# 租户与用户管理说明

## 一、不可混用的四类标识

| 名称 | 来源 | 是否可改 | 用途 | 严禁用途 |
| --- | --- | --- | --- | --- |
| `tenant_id` | Tenant Catalog | 创建后不可改 | 数据、策略、预算、发布与审计隔离键 | 登录名、人员主键 |
| `identity_id` | Keycloak Admin API | 不可改 | IdP 管理对象的定位键 | 任务归属展示字段 |
| `user_id` | OIDC `sub` | 不可改 | 平台任务归属、审批人、审计主体与权限主体 | 可读登录名 |
| `username` | Keycloak | 可改 | 登录与页面展示 | 任务归属、授权比较、外键 |

当前 Keycloak 的 `identity_id` 与 OIDC `sub` 通常相同，但代码仍显式区分二者：前者只出现在 IdP 管理 API 路径中，后者才会下发到 Runtime、Governance、RAG 和 Tool Gateway。

## 二、租户生命周期

Tenant Catalog 位于 Control Plane，并以 PostgreSQL/SQLite 的 `tenants` 表持久化。创建操作会在同一事务中写入：租户目录、默认 Tenant Policy 和 `TenantCreated` Outbox 事件。租户不提供物理删除，以保证历史 Run、发布快照、对象存储和审计记录始终能解析原始 `tenant_id`。

| 状态 | 可新增人员分配 | 保留历史 | 管理动作 |
| --- | --- | --- | --- |
| `active` | 是 | 是 | 可正常修改元数据和策略 |
| `suspended` | 否 | 是 | 用于账务、安全或合规调查后的临时冻结 |
| `retired` | 否 | 是 | 终止服务后的长期归档状态 |

只有 `platform-super-admin` 可以创建、枚举、冻结或退休租户。该要求在 Web BFF 和 Control Plane 两层校验；BFF 所附带的工作负载 `agent-admin` 角色不能替代人的平台最高权限。

## 三、人员管理流程

1. 最高管理员先创建并启用 Tenant Catalog 中的租户。
2. 在 IdP 创建人类登录身份；创建时记录 `identity_id`，首次登录取得稳定 OIDC `sub`。
3. 管理员从**已启用租户下拉框**分配人员，而不是手工输入字符串。
4. 管理员选择角色模板，再按职责授予细粒度权限。
5. BFF 以独立服务账号更新 Keycloak；浏览器不会获得 IdP 管理凭据。
6. Keycloak 立即注销被修改人员的旧会话；其下一次请求必须重新登录并取得新 Claims。
7. Control Plane、Runtime、Governance 的任务和审计事件只记录 `user_id` 与 `tenant_id`；页面按需显示 `username`。

平台管理员不能将服务账号、`platform-workload` 身份或受保护的最高管理员账号转为普通用户，也不能通过页面授予 `platform-super-admin`。这是为了避免浏览器层的误操作扩大为平台接管。

## 四、已实现与下一阶段

当前已实现：租户创建/列表/更新/冻结/退休、默认策略、Outbox 审计、租户下拉人员分配、用户启停、角色/权限分配、旧会话撤销、跨租户管理员限制和最高管理员保护。

下一阶段如接入企业 IdP，应将“创建用户、重置密码、邮件验证、组织/部门同步、离职回收”交给 Keycloak Federation、SCIM 或企业目录，而不是在 Agent 平台重复保存密码。平台只消费 IdP 的稳定 `sub`、租户和授权声明，并将人员变更事件纳入审计。
