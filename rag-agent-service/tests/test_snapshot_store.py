"""快照存储测试(提交 3:快照式人工标注)。

验证:存→取→标注→落盘重载。全离线,不调模型。
"""
import tempfile
from pathlib import Path

from app.domain.models import CrossDocEvidence, CrossDocFinding, CrossDocReport
from app.storage.snapshot_store import SnapshotStore


def _make_report() -> CrossDocReport:
    return CrossDocReport(
        verdict="测试",
        document_ids=["doc_a", "doc_b"],
        consistency_findings=[
            CrossDocFinding(
                local_id="f1",
                finding_type="consistency",
                obj="灌装间",
                document_pair=["A.txt", "B.txt"],
                topic="洁净度等级",
                summary="D级 vs B级",
                evidence=[CrossDocEvidence(document_id="doc_a", filename="A.txt", quote="D级")],
            )
        ],
    )


def test_save_and_get() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = SnapshotStore(Path(d))
        saved = store.save(_make_report())
        assert saved.snapshot_id  # 自动分配 id
        got = store.get(saved.snapshot_id)
        assert got is not None
        assert got.consistency_findings[0].local_id == "f1"


def test_annotate_updates_verdict() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = SnapshotStore(Path(d))
        saved = store.save(_make_report())
        updated = store.annotate(saved.snapshot_id, "f1", verdict="rejected", note="不同产品线")
        assert updated is not None
        assert updated.consistency_findings[0].human_verdict == "rejected"
        assert updated.consistency_findings[0].human_note == "不同产品线"


def test_annotate_persists_across_reload() -> None:
    """标注落盘后,新建 store 从磁盘重载应保留标注。"""
    with tempfile.TemporaryDirectory() as d:
        store = SnapshotStore(Path(d))
        saved = store.save(_make_report())
        store.annotate(saved.snapshot_id, "f1", verdict="confirmed")
        # 新 store 从同目录重载
        store2 = SnapshotStore(Path(d))
        got = store2.get(saved.snapshot_id)
        assert got is not None
        assert got.consistency_findings[0].human_verdict == "confirmed"


def test_annotate_missing_returns_none() -> None:
    with tempfile.TemporaryDirectory() as d:
        store = SnapshotStore(Path(d))
        saved = store.save(_make_report())
        assert store.annotate(saved.snapshot_id, "nonexistent", verdict="confirmed") is None
        assert store.annotate("nonexistent", "f1", verdict="confirmed") is None
