import type { DesktopApi } from "../shared/contracts";

const electronOnly = async () => {
  throw new Error("该操作需要从 Electron 桌面应用运行");
};

export const browserPreviewApi: DesktopApi = {
  configure: async () => undefined,
  capabilities: async () => ({ service: "browser-layout-preview" }),
  pairConnector: electronOnly,
  confirmConnector: electronOnly,
  connectorStatus: electronOnly,
  revokeConnector: electronOnly,
  requestConnectorGrant: electronOnly,
  heartbeatConnector: electronOnly,
  claimConnectorTask: electronOnly,
  executeConnectorTask: electronOnly,
  connectorTaskStatus: electronOnly,
  submit: electronOnly,
  getRun: electronOnly,
  getAuditEvents: electronOnly,
  approve: electronOnly,
  cancel: electronOnly,
  sendInput: electronOnly,
  streamEvents: async () => undefined,
  stopEvents: async () => undefined,
  selectWorkspace: async () => null,
  saveFeedback: electronOnly,
  recordRun: async () => undefined,
  listRunHistory: async () => [],
  exportRun: async () => null,
  onRuntimeEvent: () => () => undefined,
  onRuntimeError: () => () => undefined,
};
