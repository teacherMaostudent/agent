import { appendFile, mkdir, readFile } from "node:fs/promises";
import path from "node:path";
import type { RunHistoryItem } from "../shared/contracts.js";

/**
 * 保存桌面端最近运行的最小索引，而非复制任务正文、证据或 Token。
 * 完整数据仍由租户隔离的 Runtime/Governance 账本管理，桌面历史只帮助用户找回 Run ID。
 */
export class RunHistoryStore {
  constructor(private readonly directory: string) {}

  async append(item: RunHistoryItem): Promise<void> {
    if (!item.runId.trim()) throw new Error("Run history requires a run ID");
    await mkdir(this.directory, { recursive: true });
    const safe = {
      runId: item.runId.slice(0, 160), agentId: item.agentId.slice(0, 160),
      environment: item.environment.slice(0, 80), status: item.status.slice(0, 80),
      updatedAt: item.updatedAt, schemaVersion: "desktop-run-history/v1",
    };
    await appendFile(path.join(this.directory, "runs.jsonl"), `${JSON.stringify(safe)}\n`, "utf8");
  }

  async list(limit = 30): Promise<RunHistoryItem[]> {
    try {
      const source = await readFile(path.join(this.directory, "runs.jsonl"), "utf8");
      const latest = new Map<string, RunHistoryItem>();
      for (const line of source.split("\n")) {
        if (!line.trim()) continue;
        try {
          const item = JSON.parse(line) as RunHistoryItem;
          if (item.runId) latest.set(item.runId, item);
        } catch { /* 损坏单行不能阻断其余本地历史。 */ }
      }
      return [...latest.values()].sort((a, b) => b.updatedAt.localeCompare(a.updatedAt)).slice(0, limit);
    } catch { return []; }
  }
}
