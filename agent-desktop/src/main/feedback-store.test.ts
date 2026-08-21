import { mkdtemp, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { afterEach, expect, it } from "vitest";
import { FeedbackStore } from "./feedback-store.js";

let directory = "";
afterEach(async () => {
  if (directory) await rm(directory, { recursive: true, force: true });
});

it("redacts credentials and local paths before feedback is persisted", async () => {
  directory = await mkdtemp(path.join(tmpdir(), "agent-feedback-"));
  const store = new FeedbackStore(directory);
  await store.append({
    runId: "run-1",
    rating: "negative",
    category: "quality",
    note: "token sk-abcdefghijklmnop at C:\\secret\\file.txt",
  });
  const saved = await readFile(path.join(directory, "feedback.jsonl"), "utf8");
  expect(saved).toContain("[REDACTED_API_KEY]");
  expect(saved).toContain("[REDACTED_LOCAL_PATH]");
  expect(saved).not.toContain("abcdefghijklmnop");
});
