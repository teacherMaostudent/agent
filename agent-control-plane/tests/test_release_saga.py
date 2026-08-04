from app.application.release_saga import ReleaseSaga, reconcile_release


def test_release_saga_is_idempotent_and_reconciles() -> None:
    calls = []
    saga = ReleaseSaga("s-1")
    saga.step("activate", lambda: calls.append("activate"))
    saga.step("activate", lambda: calls.append("duplicate"))
    assert calls == ["activate"]
    assert reconcile_release("rel-1", "rel-2").action == "repair_or_pause"
