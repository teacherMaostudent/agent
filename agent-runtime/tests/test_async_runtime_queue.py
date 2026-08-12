from pathlib import Path
from threading import Event
from time import monotonic, sleep

from agent_runtime_service.runtime.async_jobs import AsyncRunQueue


def test_async_queue_is_idempotent_and_returns_preallocated_run_id(tmp_path: Path) -> None:
    release = Event()
    calls: list[str] = []

    def execute(submission):
        calls.append(submission["run_id"])
        release.wait(timeout=2)
        return {"status": "COMPLETED", "answer": "ok"}

    queue = AsyncRunQueue(tmp_path / "runs.db", execute)
    submission = {
        "payload": {"task": "test"},
        "tenant_id": "tenant-a",
        "user_id": "user-a",
        "permissions": "rag:read",
        "request_id": "request-a",
        "trace_id": "trace-a",
    }
    first = queue.submit(dict(submission))
    second = queue.submit(dict(submission))

    assert first["run_id"] == second["run_id"]
    release.set()
    deadline = monotonic() + 2
    while monotonic() < deadline:
        current = queue.get("tenant-a", first["run_id"])
        if current and current["status"] == "COMPLETED":
            break
        sleep(0.01)
    assert current["result"]["answer"] == "ok"
    assert calls == [first["run_id"]]
    queue.close()


def test_async_submission_preserves_frozen_release_resolution(tmp_path: Path) -> None:
    """异步队列必须原样保存 API 提交时冻结的发布解析结果。"""
    received: list[dict] = []
    queue = AsyncRunQueue(tmp_path / "frozen-release.db", lambda item: received.append(item) or {"status": "COMPLETED"})
    frozen = {"release_id": "rel-a", "version_id": "version-a", "snapshot": {"agent_id": "agent-a"}}
    queue.submit(
        {
            "payload": {"task": "test"},
            "tenant_id": "tenant-a",
            "user_id": "user-a",
            "permissions": "rag:read",
            "request_id": "request-frozen",
            "trace_id": "trace-frozen",
            "release_resolution": frozen,
        }
    )
    deadline = monotonic() + 2
    while monotonic() < deadline and not received:
        sleep(0.01)
    assert received[0]["release_resolution"] == frozen
    queue.close()
