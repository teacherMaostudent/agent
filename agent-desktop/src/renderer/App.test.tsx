// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, expect, it, vi } from "vitest";
import type { DesktopApi, RuntimeEvent } from "../shared/contracts";
import { browserPreviewApi } from "./browser-preview";

let App: typeof import("./App").App;
let listener: (runId: string, event: RuntimeEvent) => void;
const stopEvents = vi.fn(async () => undefined);

beforeAll(async () => {
  // 仅替换 IPC 边界：渲染器使用真实 App，绝不连接用户目录、凭证或外部模型。
  window.agentDesktop = {
    ...browserPreviewApi,
    configure: vi.fn(async () => undefined),
    capabilities: vi.fn(async () => ({})),
    modelRoutes: vi.fn(async () => ({ session_id: "desktop_test", default_route: "deepseek-v4-flash", release_id: "rel-test", snapshot_id: "version-test", items: [{ route_name: "deepseek-v4-flash", models: ["deepseek-v4-flash"] }] })),
    getAuditEvents: vi.fn(async () => ({ items: [], status: "available" })),
    getRun: vi.fn(async (runId: string) => ({
      run_id: runId, status: runId === "run-a" ? "COMPLETED" : "LIMIT_EXCEEDED",
      result: { answer: runId, execution_plan: { plan_id: runId + "-plan" } },
    })),
    recordRun: vi.fn(async () => undefined),
    listRunHistory: vi.fn(async () => [
      { runId: "run-a", agentId: "audit-a", environment: "local", status: "COMPLETED", updatedAt: "" },
      { runId: "run-b", agentId: "audit-b", environment: "local", status: "LIMIT_EXCEEDED", updatedAt: "" },
    ]),
    stopEvents,
    onRuntimeEvent: (callback) => { listener = callback; return () => undefined; },
  } satisfies DesktopApi;
  App = (await import("./App")).App;
});
afterEach(cleanup);

it("restoring another run clears old events and treats budget exhaustion as terminal", async () => {
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /验证连接/ }));
  fireEvent.click(await screen.findByRole("button", { name: /COMPLETED · audit-a/ }));
  await screen.findByText("1–2. Planner 与 Harness");
  await act(async () => listener("run-a", {
    sequence: 1, event_id: "event-a", event_type: "audit.old-run-only", status: "COMPLETED",
  }));
  expect(screen.getAllByText(/audit.old-run-only/).length).toBeGreaterThan(0);
  fireEvent.click(screen.getByRole("button", { name: /LIMIT_EXCEEDED · audit-b/ }));
  await screen.findAllByText("run-b");
  expect(screen.queryByText(/audit.old-run-only/)).toBeNull();
  expect(screen.queryByPlaceholderText("在下一安全边界补充或修正任务")).toBeNull();
  expect(screen.getByText("这次任务是否真正解决了问题？")).toBeTruthy();
  expect(stopEvents).toHaveBeenCalledWith("run-a");
  expect(screen.queryByLabelText("1–2. Planner 与 Harness报文")).toBeNull();
});

it("offers a blank custom task in addition to editable demonstration templates", () => {
  render(<App />);
  const editor = screen.getByLabelText("自定义发布任务内容") as HTMLTextAreaElement;
  expect(editor.value).toContain("controlled_scan");

  fireEvent.click(screen.getByRole("button", { name: /自定义发布任务/ }));
  expect(editor.value).toBe("");
  expect(screen.getByText("CUSTOM")).toBeTruthy();
  expect((screen.getByRole("button", { name: "开始执行" }) as HTMLButtonElement).disabled).toBe(true);

  fireEvent.change(editor, { target: { value: "汇总本季度已授权的客户反馈并给出证据。" } });
  expect(editor.value).toContain("客户反馈");
  expect(screen.getByText(/执行时解析 general-agent/)).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: /证据型研究报告/ }));
  expect(editor.value).toContain("证据 ID");
  expect(screen.getByText("TEMPLATE")).toBeTruthy();
});
