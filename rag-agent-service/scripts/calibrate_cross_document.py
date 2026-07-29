"""跨文档审查真机标定(补提交 2 的账)。

这不是 pytest(conftest 强制离线,无法调真模型)。这是独立脚本,手动跑,
用【真 DeepSeek】在 golden 集上跑 N 次,用提交 0 的评估器算真实召回/误报/方差。

为什么要它:提交 2 只用 FakeJudge 验了编排 + 一次 case1 真机。那把提交 0
造的尺子(评估器)从没在真实模型上量过。地基验收单没签字,不该往上盖第三层。

跑法(需先在 .env 配好 DEEPSEEK_API_KEY):
    cd rag-agent-service && source .venv/bin/activate
    python scripts/calibrate_cross_document.py

输出:每个 case 的召回/误报,以及 N 轮的均值±方差(看判定稳不稳)。
"""
import json
import sys
from pathlib import Path

# 让脚本能 import app.*(脚本在 scripts/ 下,项目根是上一级)。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import get_settings
from app.evaluation.cross_document_eval import aggregate, evaluate_suite
from app.knowledge.config_loader import load_numeric_topics, load_responsibility_topics
from app.retrieval.embedder import build_embedder
from app.retrieval.embedding_store import EmbeddingStore
from app.retrieval.semantic_retriever import SemanticRetriever
from app.review.cross_document_reviewer import CrossDocumentReviewer
from app.review.llm_judge import LlmJudge

GOLDEN = Path(__file__).resolve().parent.parent / "tests" / "golden" / "cross_document"
RUNS = 3  # temp=0 也可能有微小波动,跑多次看方差(Q4)


def _load_case_files(case_dir: Path) -> list[tuple[str, str, str]]:
    files = []
    for txt in sorted(case_dir.glob("*.txt")):
        files.append((txt.stem, txt.name, txt.read_text(encoding="utf-8")))
    return files


def _build_reviewer() -> CrossDocumentReviewer:
    """用真 DeepSeek judge(不是 conftest 的离线 hash)。"""
    settings = get_settings()
    if not settings.llm_api_key:
        print("未配置 DEEPSEEK_API_KEY,无法真机标定。请在 .env 配好后重试。")
        sys.exit(1)
    embedder = build_embedder(settings)
    semantic = SemanticRetriever(embedder, EmbeddingStore())
    judge = LlmJudge(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=settings.generation_model,
        timeout=settings.llm_timeout,
    )
    return CrossDocumentReviewer(semantic_retriever=semantic, judge=judge)


def _prediction_from_report(report) -> dict:
    """把 reviewer 输出转成评估器要的 {"conflicts":[{object, files}]}。

    命中判定用【受控对象 + 文件对】,不用 topic 名(模型自由抽取,字面对不齐)。
    files 优先用 document_pair(结构化身份),回退 evidence 的文件名。
    """
    return {
        "conflicts": [
            {
                "object": f.obj,
                "files": f.document_pair or [e.filename for e in f.evidence],
            }
            for f in report.consistency_findings
        ]
    }


def main() -> None:
    reviewer = _build_reviewer()
    numeric_topics = load_numeric_topics()
    responsibility_topics = load_responsibility_topics()
    case_dirs = [d for d in sorted(GOLDEN.iterdir()) if d.is_dir()]
    print(f"golden 集:{len(case_dirs)} 个 case,真机跑 {RUNS} 轮\n")

    round_reports = []
    for run in range(1, RUNS + 1):
        print(f"===== 第 {run}/{RUNS} 轮(真调 DeepSeek)=====")
        pairs = []
        for case_dir in case_dirs:
            expected = json.loads((case_dir / "expected.json").read_text(encoding="utf-8"))
            files = _load_case_files(case_dir)
            report = reviewer.review(
                files=files,
                numeric_topics=numeric_topics,
                responsibility_topics=responsibility_topics,
            )
            prediction = _prediction_from_report(report)
            pairs.append((expected, prediction))
            # 逐 case 打印,方便定位是哪个 case 漏检/误报。
            n_conf = len(report.consistency_findings)
            n_resp = len(report.responsibility_findings)
            print(f"  {case_dir.name}: 矛盾 {n_conf}、职责 {n_resp}")
            # 打印系统实际抽出的 topic 名 vs golden 标注的 topic,定位命中对不齐。
            pred_topics = [f.topic for f in report.consistency_findings]
            exp_topics = [c.get("topic", "") for c in expected.get("expected_conflicts", [])]
            if pred_topics or exp_topics:
                print(f"      预测topic={pred_topics}  标注topic={exp_topics}")
        suite = evaluate_suite(pairs)
        round_reports.append(suite)
        print(
            f"  → 有矛盾组召回 {suite.conflict_case_recall:.0%}"
            f"（{suite.conflict_case_count} 组）;"
            f" clean 组误报 {suite.clean_case_false_positives} 条"
            f"（{suite.clean_case_count} 组）;"
            f" 总精确率 {suite.overall_precision:.0%}\n"
        )

    agg = aggregate(round_reports)
    print("===== 汇总(均值±方差,方差大=判定不稳,该先改 prompt)=====")
    print(f"召回率:     {agg['recall_mean']:.0%} ± {agg['recall_stdev']:.0%}")
    print(f"clean 误报: {agg['clean_false_positive_mean']:.1f} ± {agg['clean_false_positive_stdev']:.1f} 条/轮")
    print(f"精确率:     {agg['precision_mean']:.0%} ± {agg['precision_stdev']:.0%}")
    print("\n验收线(建议):召回≥80%、clean 误报≤1/组、方差≤±10%。达不到先改 prompt 再往上盖。")


if __name__ == "__main__":
    main()
