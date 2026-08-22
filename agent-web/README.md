# Agent Web

这是 Agent Platform 的统一 Web 门户静态前端。同一入口根据服务端返回的已验证角色和权限显示三个工作区：

- **Agent Workspace**：普通业务用户提交任务、查看计划/证据/工具/模型/预算时间线、审批、取消与下载工件；
- **Agent Review**：被明确分配的专家或主管查看计划、证据正文、风险、共同审查人、转交和标注回流；
- **Platform Console**：平台人员查看服务健康、Release Catalog、质量门禁、发布/暂停/回滚和 Connector
  Artifact DLQ。

前端没有独立授权能力。页面可见性只是体验层，所有对象关系与权限必须由 BFF 和后端服务重新验证。
浏览器只调用同源 `/api/*`，不直连七个服务，也不保存服务密钥或 OIDC Access Token。

当前实现是无构建步骤的静态 HTML/CSS/JavaScript，便于在内网镜像中以最小依赖交付。生产由
`agent-web-bff` 托管，直接访问 `index.html` 仅用于视觉开发，无法绕过 BFF 完成业务调用。
