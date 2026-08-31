import { app, BrowserWindow, dialog, ipcMain } from "electron";
import { appendFileSync, existsSync, mkdirSync, writeFileSync } from "node:fs";
import { readdir, readFile, stat } from "node:fs/promises";
import { createHash } from "node:crypto";
import path from "node:path";
import { fileURLToPath } from "node:url";
import type {
  AgentRunRequest,
  RuntimeEvent,
  RuntimeConnection,
  RunHistoryItem,
  RunSnapshot,
  UserFeedback,
  WorkspacePreview,
} from "../shared/contracts.js";
import { FeedbackStore } from "./feedback-store.js";
import { RunHistoryStore } from "./run-history-store.js";
import { RuntimeClient } from "./runtime-client.js";

// 企业桌面环境中的显卡驱动、远程桌面与虚拟机经常无法稳定创建 Chromium GPU
// 子进程。硬件加速不是本执行台的核心能力，因此默认关闭，避免 GPU 连续崩溃后
// Chromium 直接结束整个应用；管理员完成兼容性验证后可显式重新启用。
if (process.env.AGENT_DESKTOP_HARDWARE_ACCELERATION !== "true") {
  app.disableHardwareAcceleration();
}

const runtime = new RuntimeClient();
const streams = new Map<string, AbortController>();
const claimedConnectorTasks = new Map<string, Record<string, unknown>>();
let selectedWorkspaceRoot = "";
const moduleDirectory = path.dirname(fileURLToPath(import.meta.url));
const compatibilityArgument = "--agent-safe-mode";
const diagnosticsDirectory = path.join(app.getPath("userData"), "diagnostics");
const compatibilityMarker = path.join(diagnosticsDirectory, "chromium-sandbox-disabled.json");
const compatibilityMode =
  process.env.AGENT_DESKTOP_FORCE_SANDBOX !== "true" &&
  (process.argv.includes(compatibilityArgument) || existsSync(compatibilityMarker));
let compatibilityRelaunchRequested = false;

if (compatibilityMode) {
  // 仅在正常沙箱已被本机 Chromium 子进程明确证明不可用后启用；Renderer 仍无 Node 权限，
  // 且只能通过受控 preload API 与主进程交互。
  app.commandLine.appendSwitch("no-sandbox");
}

function relaunchInCompatibilityMode(exitCode: number): void {
  if (compatibilityMode || compatibilityRelaunchRequested || exitCode !== -2147483645) return;
  compatibilityRelaunchRequested = true;
  console.error("Chromium 沙箱在当前 Windows 环境不可用，正在切换一次兼容模式");
  mkdirSync(diagnosticsDirectory, { recursive: true });
  writeFileSync(
    compatibilityMarker,
    JSON.stringify({ detectedAt: new Date().toISOString(), exitCode }, null, 2),
    "utf8",
  );
  app.relaunch({
    args: [
      ...process.argv.slice(1).filter((argument) => argument !== compatibilityArgument),
      compatibilityArgument,
    ],
  });
  app.exit(0);
}

function writeDiagnostic(event: string, details: unknown): void {
  try {
    mkdirSync(diagnosticsDirectory, { recursive: true });
    appendFileSync(
      path.join(diagnosticsDirectory, "process-events.jsonl"),
      `${JSON.stringify({ timestamp: new Date().toISOString(), event, details })}\n`,
      "utf8",
    );
  } catch (error) {
    // 诊断写盘失败不能阻塞用户任务，控制台仍保留原始异常用于本地调试。
    console.error("无法写入 Agent Desktop 诊断日志", error);
  }
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1080,
    minHeight: 700,
    backgroundColor: "#0b1020",
    webPreferences: {
      preload: path.join(moduleDirectory, "../preload/preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: !compatibilityMode,
    },
  });
  const devServer = process.env.VITE_DEV_SERVER_URL;
  window.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  window.webContents.on("render-process-gone", (_event, details) => {
    // 保留结构化错误，安装环境可通过 stderr/Chromium 日志区分正常退出与 Renderer 崩溃。
    console.error("Agent Desktop Renderer 进程异常退出", details);
    writeDiagnostic("render-process-gone", details);
    relaunchInCompatibilityMode(details.exitCode);
  });
  window.webContents.on("will-navigate", (event, target) => {
    const allowed = devServer ? target.startsWith(devServer) : target.startsWith("file:");
    if (!allowed) event.preventDefault();
  });
  if (devServer) void window.loadURL(devServer);
  else void window.loadFile(path.join(moduleDirectory, "../../dist/index.html"));
  return window;
}

async function previewWorkspace(root: string): Promise<WorkspacePreview> {
  const entries: WorkspacePreview["entries"] = [];
  const pending = [{ absolute: root, relative: "", depth: 0 }];
  let truncated = false;
  while (pending.length && entries.length < 250) {
    const current = pending.shift()!;
    const children = await readdir(current.absolute, { withFileTypes: true });
    for (const child of children) {
      if ([".git", "node_modules", ".venv", "dist"].includes(child.name)) continue;
      const relative = path.join(current.relative, child.name);
      const absolute = path.join(current.absolute, child.name);
      if (child.isDirectory()) {
        entries.push({ path: relative, kind: "directory" });
        if (current.depth < 2) pending.push({ absolute, relative, depth: current.depth + 1 });
      } else if (child.isFile()) {
        entries.push({ path: relative, kind: "file", size: (await stat(absolute)).size });
      }
      if (entries.length >= 250) {
        truncated = true;
        break;
      }
    }
  }
  return { rootName: path.basename(root), totalEntries: entries.length, truncated, entries };
}

function redactScanLine(value: string): string {
  return value.replace(/(?:sk|api)[-_][A-Za-z0-9_-]{12,}/gi, "[REDACTED_SECRET]")
    .replace(/[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}/g, "[REDACTED_EMAIL]");
}

async function executeControlledScan(task: Record<string, unknown>): Promise<Record<string, unknown>> {
  if (!selectedWorkspaceRoot) throw new Error("请先由用户选择本机工作区");
  if (task.tool_name !== "controlled_scan") throw new Error("当前 Connector 仅支持 controlled_scan");
  const argumentsValue = task.arguments as Record<string, unknown> | undefined;
  const query = String(argumentsValue?.query ?? argumentsValue?.pattern ?? "TODO").slice(0, 120);
  if (!query.trim()) throw new Error("扫描模式不能为空");
  const findings: Array<Record<string, unknown>> = [];
  const pending = [{ absolute: selectedWorkspaceRoot, relative: "", depth: 0 }];
  let filesScanned = 0;
  while (pending.length && filesScanned < 100 && findings.length < 100) {
    const current = pending.shift()!;
    for (const entry of await readdir(current.absolute, { withFileTypes: true })) {
      if ([".git", "node_modules", ".venv", "dist"].includes(entry.name)) continue;
      const absolute = path.join(current.absolute, entry.name);
      const relative = path.join(current.relative, entry.name);
      if (entry.isDirectory() && current.depth < 3) { pending.push({ absolute, relative, depth: current.depth + 1 }); continue; }
      if (!entry.isFile() || (await stat(absolute)).size > 1_000_000) continue;
      filesScanned += 1;
      const lines = (await readFile(absolute, "utf8")).split(/\r?\n/);
      lines.forEach((line, index) => { if (line.includes(query) && findings.length < 100) findings.push({ path: relative, line: index + 1, text: redactScanLine(line).slice(0, 500) }); });
    }
  }
  return { tool: "controlled_scan", query, files_scanned: filesScanned, findings, truncated: pending.length > 0 || findings.length >= 100 };
}

function activeWindow(): BrowserWindow {
  const window = BrowserWindow.getFocusedWindow() ?? BrowserWindow.getAllWindows()[0];
  if (!window) throw new Error("Desktop window is unavailable");
  return window;
}

function registerIpc(): void {
  const feedback = new FeedbackStore(path.join(app.getPath("userData"), "feedback"));
  const history = new RunHistoryStore(path.join(app.getPath("userData"), "history"));
  ipcMain.handle("runtime:configure", (_event, value: RuntimeConnection) => runtime.configure(value));
  ipcMain.handle("runtime:capabilities", () => runtime.capabilities());
  ipcMain.handle("runtime:model-routes", (_event, agentId: string, environment: string, sessionId: string) => runtime.modelRoutes(agentId, environment, sessionId));
  ipcMain.handle("runtime:pair", (_event, deviceName: string, capabilities: string[]) => runtime.pairConnector(deviceName, capabilities));
  ipcMain.handle("runtime:confirm-pair", (_event, connectorId: string, code: string) => runtime.confirmConnector(connectorId, code));
  ipcMain.handle("runtime:connector-status", (_event, connectorId: string) => runtime.connectorStatus(connectorId));
  ipcMain.handle("runtime:revoke-connector", (_event, connectorId: string) => runtime.revokeConnector(connectorId));
  ipcMain.handle("runtime:connector-grant", async (_event, connectorId: string, runId: string, snapshotId: string, toolName: string, toolVersion: string) => {
    const { grant: _grant, ...safeProjection } = await runtime.requestConnectorGrant(connectorId, runId, snapshotId, toolName, toolVersion);
    // Renderer 只需要知道授权已签发；一次性明文 grant 不离开受控主进程边界。
    return safeProjection;
  });
  ipcMain.handle("runtime:connector-heartbeat", (_event, connectorId: string) => runtime.heartbeatConnector(connectorId));
  ipcMain.handle("runtime:connector-task-next", async (_event, connectorId: string) => {
    const task = await runtime.claimConnectorTask(connectorId);
    if (task?.task_id) claimedConnectorTasks.set(String(task.task_id), task);
    return task;
  });
  ipcMain.handle("runtime:connector-task-execute", async (_event, connectorId: string, taskId: string) => {
    const task = claimedConnectorTasks.get(taskId);
    if (!task) throw new Error("本机未持有该 Connector 任务或领取租约已失效");
    const result = await executeControlledScan(task);
    const digest = createHash("sha256").update(JSON.stringify(result)).digest("hex");
    const grant = await runtime.requestConnectorGrant(
      connectorId, String(task.run_id), String(task.snapshot_id),
      String(task.tool_name), String(task.tool_version),
    );
    await runtime.completeConnectorTask(connectorId, taskId, { ...result, result_sha256: digest }, String(grant.grant));
    claimedConnectorTasks.delete(taskId);
    const delivery = await runtime.connectorTaskStatus(connectorId, taskId);
    return {
      task_id: taskId,
      files_scanned: result.files_scanned,
      finding_count: Array.isArray(result.findings) ? result.findings.length : 0,
      ...delivery,
    };
  });
  ipcMain.handle("runtime:connector-task-status", (_event, connectorId: string, taskId: string) => runtime.connectorTaskStatus(connectorId, taskId));
  ipcMain.handle("runtime:submit", (_event, value: AgentRunRequest) => runtime.submit(value));
  ipcMain.handle("runtime:get-run", (_event, runId: string) => runtime.getRun(runId));
  ipcMain.handle("runtime:get-audit-events", (_event, runId: string) => runtime.getAuditEvents(runId));
  ipcMain.handle("runtime:approve", (_event, runId: string, approved: boolean, reason: string) => runtime.approve(runId, approved, reason));
  ipcMain.handle("runtime:cancel", (_event, runId: string) => runtime.cancel(runId));
  ipcMain.handle("runtime:input", (_event, runId: string, message: string) => runtime.sendInput(runId, message));
  ipcMain.handle("runtime:stop-events", (_event, runId: string) => streams.get(runId)?.abort());
  ipcMain.handle("runtime:events", async (_event, runId: string, afterSequence: number) => {
    streams.get(runId)?.abort();
    const controller = new AbortController();
    streams.set(runId, controller);
    try {
      await runtime.streamEvents(runId, afterSequence, controller.signal, (event) =>
        activeWindow().webContents.send("runtime:event", runId, event),
      );
    } catch (error) {
      if (!controller.signal.aborted)
        activeWindow().webContents.send(
          "runtime:error",
          runId,
          error instanceof Error ? error.message : String(error),
        );
    } finally {
      streams.delete(runId);
    }
  });
  ipcMain.handle("workspace:select", async () => {
    const result = await dialog.showOpenDialog(activeWindow(), { properties: ["openDirectory"] });
    if (result.canceled) return null;
    selectedWorkspaceRoot = result.filePaths[0];
    return previewWorkspace(selectedWorkspaceRoot);
  });
  ipcMain.handle("feedback:save", (_event, value: UserFeedback) => feedback.append(value));
  ipcMain.handle("runtime:record-run", (_event, value: RunHistoryItem) => history.append(value));
  ipcMain.handle("runtime:list-history", () => history.list());
  ipcMain.handle("runtime:export-run", async (_event, run: RunSnapshot, events: RuntimeEvent[]) => {
    // 导出只由用户显式点击触发；桌面端不会自动把运行内容写到任意目录。
    const result = await dialog.showSaveDialog(activeWindow(), {
      title: "导出运行诊断包", defaultPath: `agent-run-${run.run_id}.json`,
      filters: [{ name: "JSON", extensions: ["json"] }],
    });
    if (result.canceled || !result.filePath) return null;
    const payload = { schemaVersion: "desktop-run-export/v1", exportedAt: new Date().toISOString(), run, events };
    writeFileSync(result.filePath, JSON.stringify(payload, null, 2), { encoding: "utf8", mode: 0o600 });
    return { path: result.filePath };
  });
}

app.whenReady().then(() => {
  registerIpc();
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("child-process-gone", (_event, details) => {
  // GPU、Utility 等子进程异常会直接影响桌面端可用性，记录类型和退出原因便于现场诊断。
  console.error("Agent Desktop 子进程异常退出", details);
  writeDiagnostic("child-process-gone", details);
  relaunchInCompatibilityMode(details.exitCode);
});

app.on("window-all-closed", () => {
  for (const stream of streams.values()) stream.abort();
  if (process.platform !== "darwin") app.quit();
});
