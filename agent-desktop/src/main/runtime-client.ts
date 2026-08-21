import type { AgentRunRequest, RuntimeConnection, RuntimeEvent, RunSnapshot } from "../shared/contracts.js";

export class RuntimeClient {
  private connection?: RuntimeConnection;

  configure(connection: RuntimeConnection): void {
    const parsed = new URL(connection.baseUrl);
    if (!['http:', 'https:'].includes(parsed.protocol)) throw new Error("Runtime URL must use HTTP(S)");
    if (parsed.pathname === "/") parsed.pathname = "/api/v1";
    this.connection = { ...connection, baseUrl: parsed.toString().replace(/\/$/, "") };
  }

  async capabilities(): Promise<Record<string, unknown>> {
    return this.request("/agent/capabilities", { method: "GET" });
  }

  async submit(payload: AgentRunRequest): Promise<RunSnapshot> {
    return this.request("/agent/interactive-runs", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  }

  async getRun(runId: string): Promise<RunSnapshot> {
    return this.request(`/agent/runs/${encodeURIComponent(runId)}`, { method: "GET" });
  }

  async getAuditEvents(runId: string): Promise<{ items: Record<string, unknown>[]; status: string }> {
    return this.request(`/agent/runs/${encodeURIComponent(runId)}/audit-events`, { method: "GET" });
  }

  async approve(runId: string, approved: boolean, reason: string): Promise<RunSnapshot> {
    return this.request(`/agent/runs/${encodeURIComponent(runId)}/resume`, {
      method: "POST",
      body: JSON.stringify({ approved, reason }),
    });
  }

  async cancel(runId: string): Promise<RunSnapshot> {
    return this.request(`/agent/runs/${encodeURIComponent(runId)}/cancel`, { method: "POST" });
  }

  async sendInput(runId: string, message: string): Promise<Record<string, unknown>> {
    return this.request(`/agent/runs/${encodeURIComponent(runId)}/inputs`, {
      method: "POST",
      body: JSON.stringify({ input_type: "steering", message }),
    });
  }

  async streamEvents(
    runId: string,
    afterSequence: number,
    signal: AbortSignal,
    onEvent: (event: RuntimeEvent) => void,
  ): Promise<void> {
    const url = this.url(
      `/agent/runs/${encodeURIComponent(runId)}/events?after_sequence=${afterSequence}`,
    );
    let response: Response | undefined;
    // 交互 API 先持久化队列任务再由 Worker 创建 Runtime Run；短暂 404 是正常竞态，
    // 客户端只在这个明确窗口内重试，其他错误仍立即暴露。
    for (let attempt = 0; attempt < 20; attempt += 1) {
      response = await fetch(url, { headers: this.headers(false), signal });
      if (response.status !== 404) break;
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (!response) throw new Error("Runtime event stream is unavailable");
    if (!response.ok || !response.body) throw await this.failure(response);
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop() ?? "";
      for (const frame of frames) {
        const data = frame.split("\n").find((line) => line.startsWith("data: "));
        if (data) onEvent(JSON.parse(data.slice(6)) as RuntimeEvent);
      }
    }
  }

  private async request<T>(path: string, init: RequestInit): Promise<T> {
    const response = await fetch(this.url(path), {
      ...init,
      headers: { ...this.headers(Boolean(init.body)), ...(init.headers ?? {}) },
    });
    if (!response.ok) throw await this.failure(response);
    return response.json() as Promise<T>;
  }

  private url(path: string): string {
    if (!this.connection) throw new Error("Runtime connection is not configured");
    return `${this.connection.baseUrl}${path}`;
  }

  private headers(json: boolean): Record<string, string> {
    if (!this.connection) throw new Error("Runtime connection is not configured");
    const headers: Record<string, string> = {
      "X-Tenant-Id": this.connection.tenantId,
      "X-User-Id": this.connection.userId,
      "X-Permissions": this.connection.permissions,
    };
    if (json) headers["Content-Type"] = "application/json";
    if (this.connection.bearerToken) headers.Authorization = `Bearer ${this.connection.bearerToken}`;
    return headers;
  }

  private async failure(response: Response): Promise<Error> {
    const text = await response.text();
    try {
      const payload = JSON.parse(text) as {
        detail?: string | { code?: string; message?: string };
      };
      const detail = payload.detail;
      if (typeof detail === "string") return new Error(`Runtime ${response.status}: ${detail}`);
      if (detail?.message) {
        const code = detail.code ? ` (${detail.code})` : "";
        return new Error(`Runtime ${response.status}${code}: ${detail.message}`);
      }
    } catch {
      // 非 JSON 响应仍使用有界原文，既保留诊断信息，也避免把代理页无限写入 UI。
    }
    return new Error(`Runtime ${response.status}: ${text.slice(0, 2000)}`);
  }
}
