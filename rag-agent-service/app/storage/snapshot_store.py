"""跨文档审查快照存储(提交 3:快照式人工标注)。

设计(对应"快照式标注,不做自动回流"的决定):
- 每次跨文档审查冻结成一份带时间戳的快照,存内存 + JSON 落盘(重启不丢)。
- 用户对每条 finding 的"确认/否决/备注"只写进【这一份快照】,local_id 只在
  快照内唯一——不追求跨快照稳定身份,因此不需要解决那个死结。
- 不聚合、不降权、不算全局误报率(那些依赖稳定身份+无偏采样,现不具备)。
  价值在:导出的报告带人工结论,可追溯、可推翻,符合 GMP human-review 闭环。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from app.domain.models import CrossDocReport


class SnapshotStore:
    def __init__(self, snapshot_dir: Path) -> None:
        self.dir = snapshot_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, CrossDocReport] = {}
        self._load_all()

    def _load_all(self) -> None:
        """启动时把已落盘的快照载入内存。"""
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                report = CrossDocReport(**data)
                self._cache[report.snapshot_id] = report
            except Exception:
                continue  # 单份损坏不影响其余

    def _path(self, snapshot_id: str) -> Path:
        return self.dir / f"{snapshot_id}.json"

    def save(self, report: CrossDocReport) -> CrossDocReport:
        if not report.snapshot_id:
            report.snapshot_id = "cross_" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
        self._cache[report.snapshot_id] = report
        self._path(report.snapshot_id).write_text(
            report.model_dump_json(indent=2), encoding="utf-8"
        )
        return report

    def get(self, snapshot_id: str) -> CrossDocReport | None:
        return self._cache.get(snapshot_id)

    def list_ids(self) -> list[str]:
        """按时间倒序(id 含时间戳,字典序即时间序)。"""
        return sorted(self._cache.keys(), reverse=True)

    def annotate(
        self, snapshot_id: str, local_id: str, verdict: str, note: str = ""
    ) -> CrossDocReport | None:
        """更新某快照内某条 finding 的人工标注。找不到返回 None。

        verdict: confirmed(确认为真) | rejected(否决/排除) | ""(清除)。
        标注只改这一份快照,不影响系统行为、不回流。
        """
        report = self._cache.get(snapshot_id)
        if report is None:
            return None
        for finding in [*report.consistency_findings, *report.responsibility_findings]:
            if finding.local_id == local_id:
                finding.human_verdict = verdict
                finding.human_note = note
                self.save(report)  # 落盘
                return report
        return None
