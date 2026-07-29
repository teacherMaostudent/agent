import re


class FieldExtractor:
    def extract_presence(self, text: str, fields: list[str]) -> dict[str, bool]:
        normalized = self._normalize(text)
        return {field: self._normalize(field) in normalized for field in fields}

    def scan_keywords(self, text: str, keywords: list[str]) -> list[str]:
        """返回文本中命中的关键词列表(用于判断模块主题是否出现)。"""
        normalized = self._normalize(text)
        return [kw for kw in keywords if self._normalize(kw) in normalized]

    def scan_risk(self, text: str, red_flags: list[str], expect: list[str]) -> tuple[list[str], list[str]]:
        """扫描红旗词(合规硬伤)和期望词(合规做法),返回 (命中的红旗, 命中的期望)。"""
        normalized = self._normalize(text)
        hit_flags = [kw for kw in red_flags if self._normalize(kw) in normalized]
        hit_expect = [kw for kw in expect if self._normalize(kw) in normalized]
        return hit_flags, hit_expect

    @staticmethod
    def _normalize(text: str) -> str:
        return re.sub(r"\s+", "", text or "").lower()

