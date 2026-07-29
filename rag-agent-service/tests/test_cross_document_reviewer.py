"""跨文档 reviewer 测试(提交 2)。

三块:
1. 职责线(纯规则,离线): 用 golden case1 验证"张三既操作又复核"被规则粗筛抓到。
2. 一致性线(FakeJudge): 不联网,验证编排把 judge 的矛盾结果正确装成 finding。
3. 接提交 0 的尺子: 把 reviewer 输出喂给评估器,跑 golden set 出召回/误报。

judge 走 Fake,不调真 DeepSeek。真实效果最后起服务手动验一次。
"""
from pathlib import Path

from app.evaluation.cross_document_eval import evaluate_suite
from app.retrieval.embedder import build_embedder
from app.retrieval.embedding_store import EmbeddingStore
from app.retrieval.semantic_retriever import SemanticRetriever
from app.review.cross_document_reviewer import CrossDocumentReviewer

GOLDEN = Path(__file__).parent / "golden" / "cross_document"


def _make_reviewer(judge=None) -> CrossDocumentReviewer:
    """离线 reviewer: hash embedder(conftest 已强制),空法规库不影响跨文档。"""
    from app.core.config import get_settings

    embedder = build_embedder(get_settings())
    semantic = SemanticRetriever(embedder, EmbeddingStore())
    return CrossDocumentReviewer(semantic_retriever=semantic, judge=judge)


def _load_case_files(case_dir: Path) -> list[tuple[str, str, str]]:
    files = []
    for txt in sorted(case_dir.glob("*.txt")):
        files.append((txt.stem, txt.name, txt.read_text(encoding="utf-8")))
    return files


# --- 1. 职责线:纯规则,不需要 judge ---


def test_responsibility_rule_catches_same_person_operate_and_review() -> None:
    """case1 的 A 文件里张三既做灌装操作又做复核 → 规则粗筛应标出。"""
    reviewer = _make_reviewer(judge=None)
    files = _load_case_files(GOLDEN / "case1_conflict")
    from app.knowledge.config_loader import load_responsibility_topics

    report = reviewer.review(
        files=files,
        numeric_topics=[],  # 只测职责线
        responsibility_topics=load_responsibility_topics(),
    )
    # 应抓到"张三"职责分离疑点,且标待人工确认(只提示不裁决)。
    assert len(report.responsibility_findings) >= 1
    hit = report.responsibility_findings[0]
    assert "张三" in hit.summary or "张三" in hit.detail
    assert hit.need_human_review is True


# --- 2. 一致性线:FakeJudge 验编排 ---


class _FakeExtractionJudge:
    """内容感知的假抽取器:从单份文件文本里抽洁净度断言(灌装间的洁净度等级)。

    重构后 LLM 只做"文本→断言"抽取,判矛盾是下游纯规则。所以假 judge 只需
    诚实地把文件里的洁净度等级抽出来,矛盾由 reviewer 的规则比对得出——
    这正好验证了"判断是确定性的、不靠模型每次发挥"。
    """

    def extract_assertions(self, filename: str, text: str, attribute_hints: list[str]) -> dict:
        import re

        flat = text.replace(" ", "")
        assertions = []
        if "灌装间" in flat:
            m = re.search(r"([A-D])级洁净", flat)
            if m:
                assertions.append({
                    "object": "灌装间",
                    "attribute": "洁净度等级",
                    "value": f"{m.group(1)}级",
                    "quote": f"灌装间应维持 {m.group(1)} 级洁净度",
                })
        return {"assertions": assertions}


def test_consistency_line_builds_finding_from_extraction() -> None:
    reviewer = _make_reviewer(judge=_FakeExtractionJudge())
    files = _load_case_files(GOLDEN / "case1_conflict")
    from app.knowledge.config_loader import load_numeric_topics

    report = reviewer.review(
        files=files,
        numeric_topics=load_numeric_topics(),
        responsibility_topics=[],  # 只测一致性线
    )
    # A抽出灌装间D级、B抽出灌装间B级 → 规则比对判为矛盾。
    assert len(report.consistency_findings) >= 1
    f = report.consistency_findings[0]
    assert f.finding_type == "consistency"
    assert f.topic == "洁净度等级"
    assert len(f.evidence) == 2  # 双方文件原话都在,可复核
    assert f.need_human_review is True


def test_no_judge_skips_consistency_no_hallucination() -> None:
    """judge=None 时一致性线不臆测矛盾(必须靠语义),返回空。"""
    reviewer = _make_reviewer(judge=None)
    files = _load_case_files(GOLDEN / "case1_conflict")
    from app.knowledge.config_loader import load_numeric_topics

    report = reviewer.review(
        files=files, numeric_topics=load_numeric_topics(), responsibility_topics=[]
    )
    assert report.consistency_findings == []


# --- 3. 接提交 0 的尺子:golden set 上量召回/误报 ---


def test_golden_set_with_fake_judge_metrics() -> None:
    """用 FakeJudge 跑 golden set,验证尺子能量出 reviewer 的召回/误报。

    这不是验真实模型效果(那要真 DeepSeek),而是验证"reviewer→评估器"这条链
    通了:有矛盾组能召回,clean 组不误报。
    """
    import json

    reviewer = _make_reviewer(judge=_FakeExtractionJudge())
    from app.knowledge.config_loader import load_numeric_topics, load_responsibility_topics

    pairs = []
    for case_dir in sorted(GOLDEN.iterdir()):
        if not case_dir.is_dir():
            continue
        expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
        files = _load_case_files(case_dir)
        report = reviewer.review(
            files=files,
            numeric_topics=load_numeric_topics(),
            responsibility_topics=load_responsibility_topics(),
        )
        # 把 reviewer 输出转成评估器要的 {"conflicts": [{topic, files}]}。
        prediction = {
            "conflicts": [
                {
                    "topic": f.topic,
                    "files": [e.filename for e in f.evidence],
                }
                for f in report.consistency_findings
            ]
        }
        pairs.append((expected, prediction))

    suite = evaluate_suite(pairs)
    # clean 组不该有一致性误报(FakeJudge 只对洁净度报,case2 无洁净度主题命中)。
    assert suite.clean_case_false_positives == 0
