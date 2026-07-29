from app.domain.models import RiskLevel


class RiskScorer:
    def score(self, severity: RiskLevel, missing_count: int) -> RiskLevel:
        if missing_count == 0:
            return RiskLevel.LOW
        if severity in {RiskLevel.CRITICAL, RiskLevel.HIGH} and missing_count >= 2:
            return RiskLevel.HIGH
        if severity == RiskLevel.HIGH:
            return RiskLevel.MEDIUM
        return RiskLevel.MEDIUM if missing_count >= 2 else RiskLevel.LOW

    def overall(self, levels: list[RiskLevel]) -> RiskLevel:
        order = [RiskLevel.LOW, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.CRITICAL]
        return max(levels or [RiskLevel.LOW], key=lambda level: order.index(level))

