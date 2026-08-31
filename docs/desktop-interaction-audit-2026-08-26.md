# Desktop 真实交互测试记录（2026-08-26）

> 2026-08-27 增量复测：打包后的 Electron 新增“自定义发布任务”空白入口，并验证任务
> 来源标记、编辑、Agent/环境提示及模板往返切换。连同原有真实 Runtime、报告折叠、反馈、
> 历史恢复、Connector、受控扫描、错误、Steering/取消和空任务约束，共 **15/15** 通过。
> 原生目录/保存对话框、真实审批恢复、Connector 扫描进入 RAG、生产 OIDC 与强制沙箱仍不
> 包含在该数字内。

## 测试对象和方法

本轮运行的是 `agent-desktop/release/win-unpacked/Agent Workbench.exe`，不是浏览器预览页，
也不是 jsdom/IPC 替身。自动化驱动真实 Electron 页面按钮、preload、主进程、文件反馈存储以及本地 Runtime/Gateway。
每轮使用新建的 `agent-desktop-e2e-*` 临时配置目录和 `audit-desktop-*` 测试租户；未操作或关闭用户已有 Workbench 实例。
新建的测试发布不覆盖 `demo/general-agent`，测试中的模型调用会产生正常的小额用量。

在本机以 `--agent-safe-mode` 启动，确认 contextIsolation=true、nodeIntegration=false。
**这是兼容模式测试，不能用于证明 Chromium 沙箱启用时也已验收。**

## 最终通过：14 项真实 Electron 交互

| 场景 | 实际断言 |
| --- | --- |
| 安装产物启动 | 真实打包程序出现主界面，未连接时执行按钮禁用；隔离偏好符合预期 |
| 错误连接地址 | ftp 地址给出明确错误，不能开始执行 |
| Runtime 连接 | 点击验证后，通过真实服务返回能力，显示已连接 |
| 演示模板 | 证据报告/文件整理按钮能替换任务文本 |
| 实际任务执行 | 提交算术任务，进入 COMPLETED，答案包含 4，显示 Run ID 和事件时间线 |
| 八组报文详情 | 每组默认不挂载 JSON；点击后显示可解析的完整报文，再点击关闭 |
| 反馈持久化 | 点击反馈后实际写入隔离 profile 的 JSONL；合成 API Key 被脱敏，原文未落盘 |
| 终态控制 | 完成后不再显示取消按钮和 Steering 输入框 |
| 历史恢复 | 重新加载渲染器后验证连接，从真实本地索引恢复原 Run；详情默认折叠 |
| Connector | 生成配对码、确认、读取连接状态、撤销，并显示不可恢复提示 |
| Runtime 实际调工具 | 通过桌面任务触发 controlled_scan，详情包含成功工具结果和 dispatch 事件；不是测试脚本直接调用工具 |
| 不存在的发布 | 提交不存在的 Agent 得到 agent_release_not_found，之前运行结果仍保留 |
| Steering 与取消 | 活动 Run 接收补充指令，输入框清空；再取消，最终为 CANCELLED，控制入口消失 |
| 空白任务 | 全空格任务不能提交 |

最终本轮配置：

- 租户：`audit-desktop-1787758289824`
- 工具测试 Run：`run_a737b47db4314070a727883832652511`
- 报告：`C:\Users\Administrator\AppData\Local\Temp\agent-desktop-e2e-dmnkxK\report.json`
- 自动化进程退出码 0；该轮 pageErrors 为空。
- 命令包含 `--without-native --with-tool`，所以 **14/14 仅指上述子集，不包含原生文件选择器等未验证项**。

受控扫描使用的是服务端演示 Scope。它不是用户电脑任意目录的扫描，也不等同于 Connector 本机扫描→审批→RAG 摄取的整条链路。

## 原生选择器与诊断导出：部分有证据，仍未完成自动化验收

目录选择取消、目录选择确认、导出取消、导出写盘这四项，在完整测试中未能由 Windows UI Automation 捕获到目标原生对话框。
完整尝试最终为 **13 通过 / 4 未通过**，对应目录 `agent-desktop-e2e-enbysJ`。
在点击前启动监听，以及改用 Win32 HWND 按测试进程定位后，仍未完成自动化验收。

进一步的只读追踪保留原始 `dialog.showOpenDialog` 的行为，不伪造选择结果：
它确实被调用，并返回 `canceled=false, pathCount=1`。因此不能仅根据“自动化未找到窗口”就断定产品不能选择目录；
需要确认是否有人工操作或本机原生窗口交互差异。**本报告没有把这四项改算为通过。**

测试期间实际生成的两份诊断文件已经核对：

| 文件（项目 docs 目录） | 核验结果 |
| --- | --- |
| agent-run-run_277085d0a3dc44c081512435f4c566f9.json | desktop-run-export/v1；Run ID 对应合成任务；COMPLETED；17 条事件 |
| agent-run-run_3345192c94be4d23b09296f452af3374.json | desktop-run-export/v1；Run ID 对应合成任务；COMPLETED；17 条事件 |

这证明诊断导出已有真实落盘结果，但不能证明自动化完成了选择位置、取消和覆盖确认的所有分支。
原始诊断包含运行上下文，不应直接作为公开 Git 附件提交；本轮保留本地文件，没有上传或删除它们。

早期测试还出现过一次截图超时，以及一次错误地址点击后未及时出现提示；后续顺序复测通过。
这些原始失败没有从报告目录删除，不能据最终单轮通过宣称交互完全无抖动。

## 发现的版本问题

当前用户已安装程序与本轮测试的新构建虽然均标记为 **0.1.5**，但内容不同：

| 程序 | app.asar SHA-256 | 折叠详情组件 |
| --- | --- | --- |
| release/win-unpacked 新构建 | C5AB02FC64FEF7608481CCAA82EF304E8450831E7255719F278FF742B02B23D8 | 有 |
| AppData/Local/Programs/agent-desktop 已安装版本 | 05496BFB42ADC50723770C60F5471F62B7F245FB118650A33992F391A123FEA0 | 无 |

因此，仍运行已安装旧实例时看不到新折叠布局是可解释的，不能通过刷新窗口解决。
应退出旧程序后安装本轮已有的 `agent-desktop/release/Agent Workbench Setup 0.1.5.exe`。
本轮没有擅自替用户安装。后续正式发布应提升版本或展示构建标识，避免同版本号不同内容难以辨认。

## 仍未覆盖

1. 真实 WAITING_APPROVAL → 批准/拒绝 → 恢复：当前本地工具目录没有要求审批的工具，不能随意改现有目录或伪造 UI 状态冒充验收。
2. Connector 本机目录扫描、结果授权回传、Web 审批、RAG 摄取：目录选择自动化尚未确认；没有用模拟目录响应替代。
3. Desktop 的真实 OIDC Token 过期、轮换及跨用户权限回收；本轮使用明确的本地兼容身份模式。
4. 原生安装向导、升级/卸载、签名校验、多显示器/缩放/长时间运行及启用沙箱的环境矩阵。
5. 重新启动整个主进程后的历史恢复：本轮验证的是渲染器 reload + 实际磁盘索引，不应写成完整应用重启恢复。

## 可重复执行的入口

已增加：

- `agent-desktop/scripts/e2e.mjs`：真实打包程序测试，不替换 fetch/IPC。
- `agent-desktop/scripts/native-dialog.ps1`：仅匹配指定测试进程的原生对话框，目标路径限定测试临时目录。
- `agent-desktop/scripts/native-dialog-probe.mjs`：保留原生选择行为的诊断追踪，不伪造返回值。

需要本地服务已运行、已生成 win-unpacked，且本机安装 Playwright；或通过 `PLAYWRIGHT_MODULE` 指向已有的 `playwright/index.mjs`。

```powershell
Set-Location "C:\Users\Administrator\Documents\AI工作\agent\agent-desktop"
# 全部已编写场景，包含原生窗口尝试和真实工具调用
node scripts/e2e.mjs --with-tool
# 仅复测已验证的非原生窗口子集；报告会明确列出四项跳过
node scripts/e2e.mjs --without-native --with-tool
```

本轮只新增测试和说明，没有修改产品业务逻辑，也没有提交或推送 Git。
