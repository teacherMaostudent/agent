export type RuntimeConnection = {
  baseUrl: string;
  tenantId: string;
  userId: string;
  permissions: string;
  bearerToken?: string;
};

export type AgentRunRequest = {
  task: string;
  agent_id: string;
  environment: string;
  session_id?: string;
  max_steps?: number;
  max_cost_usd?: number;
  metadata: Record<string, unknown>;
};

export type RunSnapshot = {
  run_id: string;
  agent_id?: string;
  snapshot_id?: string;
  context?: Record<string, unknown>;
  status: string;
  result?: Record<string, unknown>;
  error?: string;
  cancel_requested?: boolean;
};

export type RuntimeEvent = {
  sequence: number;
  event_id: string;
  event_type: string;
  status?: string;
  metadata?: Record<string, unknown>;
  model_message?: { role?: string; content?: string };
  created_at?: string;
};

export type WorkspacePreview = {
  rootName: string;
  totalEntries: number;
  truncated: boolean;
  entries: Array<{ path: string; kind: "file" | "directory"; size?: number }>;
};

export type UserFeedback = {
  runId: string;
  rating: "positive" | "negative";
  category: "quality" | "tool" | "latency" | "safety" | "ux";
  note: string;
};

/** 本地历史只保留运行索引，完整业务内容仍以 Runtime 与治理账本为准。 */
export type RunHistoryItem = {
  runId: string;
  agentId: string;
  environment: string;
  status: string;
  updatedAt: string;
};

export type DesktopApi = {
  configure(connection: RuntimeConnection): Promise<void>;
  capabilities(): Promise<Record<string, unknown>>;
  submit(payload: AgentRunRequest): Promise<RunSnapshot>;
  getRun(runId: string): Promise<RunSnapshot>;
  getAuditEvents(runId: string): Promise<{ items: Record<string, unknown>[]; status: string }>;
  approve(runId: string, approved: boolean, reason: string): Promise<RunSnapshot>;
  cancel(runId: string): Promise<RunSnapshot>;
  sendInput(runId: string, message: string): Promise<Record<string, unknown>>;
  streamEvents(runId: string, afterSequence: number): Promise<void>;
  stopEvents(runId: string): Promise<void>;
  selectWorkspace(): Promise<WorkspacePreview | null>;
  saveFeedback(feedback: UserFeedback): Promise<{ feedbackId: string; storedAt: string }>;
  recordRun(item: RunHistoryItem): Promise<void>;
  listRunHistory(): Promise<RunHistoryItem[]>;
  exportRun(run: RunSnapshot, events: RuntimeEvent[]): Promise<{ path: string } | null>;
  onRuntimeEvent(listener: (runId: string, event: RuntimeEvent) => void): () => void;
  onRuntimeError(listener: (runId: string, message: string) => void): () => void;
};
