import { afterEach, describe, expect, it, vi } from "vitest";
import { RuntimeClient } from "./runtime-client.js";

describe("RuntimeClient", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("keeps identity in the Electron main process and submits to the interactive API", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ run_id: "run-1", status: "QUEUED" }), { status: 202, headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    const client = new RuntimeClient();
    client.configure({ baseUrl: "http://localhost:8000", tenantId: "t1", userId: "u1", permissions: "rag:read", bearerToken: "secret" });
    await client.submit({ task: "test", agent_id: "agent-a", environment: "production", metadata: {} });
    const [url, request] = fetchMock.mock.calls[0];
    expect(url).toBe("http://localhost:8000/api/v1/agent/interactive-runs");
    expect(request.headers.Authorization).toBe("Bearer secret");
    expect(request.headers["X-Tenant-Id"]).toBe("t1");
  });

  it("retries the queue-to-run visibility gap before consuming SSE", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(new Response("not ready", { status: 404 }))
      .mockResolvedValueOnce(
        new Response(
          'data: {"sequence":1,"event_id":"event-1","event_type":"runtime.run.started"}\n\n',
          { status: 200 },
        ),
      );
    vi.stubGlobal("fetch", fetchMock);
    const client = new RuntimeClient();
    client.configure({
      baseUrl: "http://localhost:8000/api/v1",
      tenantId: "t1",
      userId: "u1",
      permissions: "rag:read",
    });
    const events: string[] = [];

    await client.streamEvents(
      "run-1",
      0,
      new AbortController().signal,
      (event) => events.push(event.event_id),
    );

    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(events).toEqual(["event-1"]);
  });

  it("shows the stable Runtime error code instead of an opaque server failure", async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(
        JSON.stringify({
          detail: {
            code: "agent_release_not_found",
            message: "No active release exists for this Agent.",
          },
        }),
        { status: 404, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.stubGlobal("fetch", fetchMock);
    const client = new RuntimeClient();
    client.configure({
      baseUrl: "http://localhost:8000/api/v1",
      tenantId: "t1",
      userId: "u1",
      permissions: "rag:read",
    });

    await expect(
      client.submit({ task: "test", agent_id: "missing-agent", environment: "local" }),
    ).rejects.toThrow(
      "Runtime 404 (agent_release_not_found): No active release exists for this Agent.",
    );
  });
});
