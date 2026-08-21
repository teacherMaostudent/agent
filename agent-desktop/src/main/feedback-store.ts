import { randomUUID } from "node:crypto";
import { appendFile, mkdir } from "node:fs/promises";
import path from "node:path";
import type { UserFeedback } from "../shared/contracts.js";

export class FeedbackStore {
  constructor(private readonly directory: string) {}

  async append(feedback: UserFeedback): Promise<{ feedbackId: string; storedAt: string }> {
    if (!feedback.runId.trim()) throw new Error("Feedback requires a run ID");
    const storedAt = new Date().toISOString();
    const feedbackId = `feedback_${randomUUID()}`;
    const record = {
      feedbackId,
      storedAt,
      runId: feedback.runId.slice(0, 160),
      rating: feedback.rating,
      category: feedback.category,
      note: redact(feedback.note).slice(0, 2000),
      schemaVersion: "desktop-feedback/v1",
    };
    await mkdir(this.directory, { recursive: true });
    await appendFile(path.join(this.directory, "feedback.jsonl"), `${JSON.stringify(record)}\n`, {
      encoding: "utf8",
      mode: 0o600,
    });
    return { feedbackId, storedAt };
  }
}

function redact(value: string): string {
  return value
    .replace(/\b(sk-[A-Za-z0-9_-]{12,})\b/g, "[REDACTED_API_KEY]")
    .replace(/\b(Bearer\s+[A-Za-z0-9._-]{12,})\b/gi, "[REDACTED_TOKEN]")
    .replace(/([A-Za-z]:\\[^\s]+)/g, "[REDACTED_LOCAL_PATH]");
}
