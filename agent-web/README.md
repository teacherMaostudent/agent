# Agent Web

Agent Web 是企业 Agent Platform 的统一浏览器入口。它把同一套后端能力投影成面向业务用户、
审查专家和平台管理员的三种工作区，但不在浏览器内复制权限判断、Runtime 状态机或发布逻辑。

## 为什么需要统一 Web 门户

如果每个业务 Agent 都单独建设页面，登录、任务列表、审批、证据展示、审查和运维视图会迅速重复，
而且容易出现同一个用户在不同页面拥有不一致权限。统一门户的价值是复用交互骨架和身份会话，同时
依据服务端返回的角色、权限、租户和对象关系展示不同能力。它统一体验，不统一业务语义；每个 Agent
仍由自己的 Release、Snapshot、Skill、Tool 和治理策略定义行为。

## 三类工作区

- **Agent Workspace**：普通业务用户提交任务、查看计划/证据/工具/模型/预算时间线、审批、取消与下载工件；
- **Agent Review**：被明确分配的专家或主管查看计划、证据正文、风险、共同审查人、转交和标注回流；
- **Platform Console**：平台人员查看服务健康、Release Catalog、质量门禁、发布/暂停/回滚和 Connector
  Artifact DLQ；维护模型路由 Release 与租户/用户 Token、成本配额；发起和追踪 WORM 审计导出。

页面不会给无权限用户展示不可用菜单。菜单可见性由 BFF 返回的能力投影决定，但隐藏菜单只是体验层，
不是安全措施；用户即使手工构造请求，BFF 和目标服务仍会重新授权。

## 用户任务链路

```text
Browser → Web BFF 会话校验 → Runtime 创建 Run
        → SSE/轮询获取计划、证据、模型、工具、成本和状态事件
        → 用户审批、Steering、取消或下载 Artifact
        → BFF 按权限调用 Runtime / Governance / Control Plane
```

任务列表采用服务端分页，不把所有历史任务一次性加载到浏览器。任务详情默认展示面向人的摘要，计划、
Evidence、Tool Observation、模型路由和原始事件在需要时展开，避免把内部报文直接堆叠成运维日志页面。

## Multi-Agent 展示方式

Web 不负责决定由几个 Agent 协作。Runtime 返回父子 Run、Session、Agent ID 和 Release/Snapshot 关联后，
Workspace 将其投影为一个业务任务下的协作步骤，Review 按责任人和风险展示待审对象，Console 才展示
完整拓扑与跨 Agent 事件。这样业务用户看到结果和进度，专家看到证据与责任边界，平台人员看到调度与治理。

## Artifact、知识晋升与高风险操作

Workspace 对允许的文本 Artifact 提供对象存储有界预览和同一逻辑产物的版本差异。Review/Workspace
可以审批 `controlled_scan` 的知识晋升请求；批准只创建耐久摄取任务，不允许浏览器或 Desktop 直接写索引。
WORM 导出、模型路由和配额都是高风险 Console 操作，BFF 会再次校验独立 permission、近期 MFA/ACR、
目标 ID 回显和变更原因，隐藏菜单不构成授权。

## 身份与权限边界

前端没有独立授权能力。页面可见性只是体验层，所有对象关系与权限必须由 BFF 和后端服务重新验证。
浏览器只调用同源 `/api/*`，不直连七个服务，也不保存服务密钥或 OIDC Access Token。

租户、用户账号和业务 `user_id` 是不同概念：IdP 负责登录主体，平台租户成员关系决定其可访问的数据域，
业务请求中的 `user_id` 仅用于标识已验证主体，不能由输入框自行声明。管理员修改成员、角色或权限时，
操作通过独立管理 API 落库并写治理审计事件。

## 交付与运行

当前实现是无构建步骤的静态 HTML/CSS/JavaScript，便于在内网镜像中以最小依赖交付。生产由
`agent-web-bff` 托管，直接访问 `index.html` 仅用于视觉开发，无法绕过 BFF 完成业务调用。

本地一键启动后默认访问 `http://127.0.0.1:9010`。生产部署应由反向代理统一 TLS 域名，设置严格 CSP，
并让浏览器只访问 BFF；不得把 Runtime、Control Plane 或 Governance 的内部端口公开给普通用户。

## 当前边界

- Web 是交付入口，不是新的第八个决策服务；
- 它不直接读取数据库、对象存储或 Kafka；
- 它不根据前端角色自行放行发布、审查或工具调用；
- 前端视觉回归不能替代 OIDC、租户隔离、对象级授权和完整 E2E 验证。
