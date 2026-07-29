from app.domain.models import ChecklistItem, RiskLevel


class CapaGenerator:
    def generate(self, item: ChecklistItem, missing_points: list[str], risk_level: RiskLevel) -> str:
        if not missing_points:
            return "保持现有控制措施，按既定周期复核文件有效性。"
        owner = "QA 负责人" if risk_level in {RiskLevel.HIGH, RiskLevel.CRITICAL} else "文件责任人"
        points = "、".join(missing_points)
        return f"由{owner}补充或修订「{points}」，完成影响评估、版本批准和培训确认，并在下次内审中验证有效性。"

