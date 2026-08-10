"""Region-aware Temporal target selection.

Targets are supplied as a JSON object such as ``{"cn":"temporal-cn:7233"}``.
The default target remains the local/primary cluster, so existing deployments
need no change.
"""

from __future__ import annotations

import json


class TemporalTargetRouter:
    def __init__(self, primary: str, targets_json: str = "") -> None:
        self.primary = primary
        try:
            parsed = json.loads(targets_json) if targets_json else {}
        except json.JSONDecodeError as exc:
            raise ValueError("temporal region targets must be valid JSON") from exc
        if not isinstance(parsed, dict) or any(
            not isinstance(k, str) or not isinstance(v, str) for k, v in parsed.items()
        ):
            raise ValueError("temporal region targets must be a string-to-string JSON object")
        self.targets = {k: v for k, v in parsed.items() if v}

    def target_for(self, region: str | None) -> str:
        return self.targets.get((region or "").strip(), self.primary)

    def candidates(self, region: str | None) -> list[str]:
        preferred = self.target_for(region)
        return list(dict.fromkeys([preferred, self.primary, *self.targets.values()]))

    @staticmethod
    def task_queue_for(base_queue: str, region: str | None) -> str:
        normalized = (region or "").strip().lower()
        return f"{base_queue}-{normalized}" if normalized else base_queue
