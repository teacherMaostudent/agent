import { contextBridge, ipcRenderer } from "electron";
import type {
  AgentRunRequest,
  DesktopApi,
  RuntimeConnection,
  RuntimeEvent,
  RunHistoryItem,
  RunSnapshot,
  UserFeedback,
} from "../shared/contracts.js";

// Preload 使用 CommonJS 输出以兼容 Electron 的沙箱加载器，并只暴露显式 IPC 能力，
// 不向 Renderer 泄露 ipcRenderer、文件系统或 Runtime 凭据。
const api: DesktopApi = {
  configure: (value: RuntimeConnection) => ipcRenderer.invoke("runtime:configure", value),
  capabilities: () => ipcRenderer.invoke("runtime:capabilities"),
  pairConnector: (deviceName: string, capabilities: string[]) => ipcRenderer.invoke("runtime:pair", deviceName, capabilities),
  confirmConnector: (connectorId: string, pairingCode: string) => ipcRenderer.invoke("runtime:confirm-pair", connectorId, pairingCode),
  connectorStatus: (connectorId: string) => ipcRenderer.invoke("runtime:connector-status", connectorId),
  revokeConnector: (connectorId: string) => ipcRenderer.invoke("runtime:revoke-connector", connectorId),
  requestConnectorGrant: (connectorId: string, runId: string, snapshotId: string, toolName: string, toolVersion: string) => ipcRenderer.invoke("runtime:connector-grant", connectorId, runId, snapshotId, toolName, toolVersion),
  heartbeatConnector: (connectorId: string) => ipcRenderer.invoke("runtime:connector-heartbeat", connectorId),
  claimConnectorTask: (connectorId: string) => ipcRenderer.invoke("runtime:connector-task-next", connectorId),
  executeConnectorTask: (connectorId: string, taskId: string) => ipcRenderer.invoke("runtime:connector-task-execute", connectorId, taskId),
  connectorTaskStatus: (connectorId: string, taskId: string) => ipcRenderer.invoke("runtime:connector-task-status", connectorId, taskId),
  submit: (value: AgentRunRequest) => ipcRenderer.invoke("runtime:submit", value),
  getRun: (runId: string) => ipcRenderer.invoke("runtime:get-run", runId),
  getAuditEvents: (runId: string) => ipcRenderer.invoke("runtime:get-audit-events", runId),
  approve: (runId: string, approved: boolean, reason: string) =>
    ipcRenderer.invoke("runtime:approve", runId, approved, reason),
  cancel: (runId: string) => ipcRenderer.invoke("runtime:cancel", runId),
  sendInput: (runId: string, message: string) =>
    ipcRenderer.invoke("runtime:input", runId, message),
  streamEvents: (runId: string, afterSequence: number) =>
    ipcRenderer.invoke("runtime:events", runId, afterSequence),
  stopEvents: (runId: string) => ipcRenderer.invoke("runtime:stop-events", runId),
  selectWorkspace: () => ipcRenderer.invoke("workspace:select"),
  saveFeedback: (value: UserFeedback) => ipcRenderer.invoke("feedback:save", value),
  recordRun: (value: RunHistoryItem) => ipcRenderer.invoke("runtime:record-run", value),
  listRunHistory: () => ipcRenderer.invoke("runtime:list-history"),
  exportRun: (run: RunSnapshot, events: RuntimeEvent[]) =>
    ipcRenderer.invoke("runtime:export-run", run, events),
  onRuntimeEvent: (listener: (runId: string, event: RuntimeEvent) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, runId: string, value: RuntimeEvent) =>
      listener(runId, value);
    ipcRenderer.on("runtime:event", handler);
    return () => ipcRenderer.removeListener("runtime:event", handler);
  },
  onRuntimeError: (listener: (runId: string, message: string) => void) => {
    const handler = (_event: Electron.IpcRendererEvent, runId: string, value: string) =>
      listener(runId, value);
    ipcRenderer.on("runtime:error", handler);
    return () => ipcRenderer.removeListener("runtime:error", handler);
  },
};

contextBridge.exposeInMainWorld("agentDesktop", api);
