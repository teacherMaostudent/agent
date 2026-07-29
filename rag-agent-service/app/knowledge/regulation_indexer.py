"""法规库建库(设计文档组件① · 交接文档优先级2)。

把纳入的真实法规 PDF → 解析 → 切块 → 打标签(standard/source/language)→
embedding → 存进 EmbeddingStore → 落地本地缓存。embedding 只在建库时算一次,
重启直接加载缓存,不重算、不重复花钱(呼应 Java GmpIngestionService)。

纳入的 5 部法规(已与用户确认,2026-07-09):
- 中国GMP 2010(中文)
- ICH Q10 中文
- ISO 9001
- 21 CFR Part 210(2026 英文最新)
- 21 CFR Part 211(2026 英文最新)

FDA 210-211 2020 合订版(旧)、Q10 英文版(与中文重复)、正大天晴扫描版测试件
(是被审查对象,非法规,且需 OCR)均不纳入,避免检索结果重复。
"""
import logging
from pathlib import Path

from app.domain.models import Chunk
from app.ingestion.chunker import TextChunker
from app.ingestion.parsers import DocumentParser
from app.retrieval.embedding_store import EmbeddingStore

log = logging.getLogger(__name__)


# 纳入法规清单:文件名(在 regulation_source_dir 下) → 标签。
# 文件名须与磁盘完全一致;缺失的文件会被跳过并告警,不影响其余建库。
REGULATION_SOURCES: list[dict] = [
    {
        "filename": "药品生产质量管理规范（2010 年修订）.pdf",
        "standard": "中国GMP-2010",
        "regulation": "中国药品生产质量管理规范",
        "language": "zh",
    },
    {
        "filename": "Q10：药品质量体系.pdf",
        "standard": "ICH-Q10",
        "regulation": "ICH Q10 药品质量体系",
        "language": "zh",
    },
    {
        "filename": "ISO 9001.pdf",
        "standard": "ISO-9001",
        "regulation": "ISO 9001 质量管理体系",
        "language": "en",
    },
    {
        "filename": "21 CFR Part 210 (up to date as of 6-26-2026).pdf",
        "standard": "FDA-cGMP-210",
        "regulation": "21 CFR Part 210",
        "language": "en",
    },
    {
        "filename": "21 CFR Part 211 (up to date as of 6-26-2026).pdf",
        "standard": "FDA-cGMP-211",
        "regulation": "21 CFR Part 211",
        "language": "en",
    },
]


class RegulationIndexer:
    def __init__(
        self,
        embedder,
        parser: DocumentParser | None = None,
        chunker: TextChunker | None = None,
    ) -> None:
        self.embedder = embedder
        self.parser = parser or DocumentParser()
        self.chunker = chunker or TextChunker()

    def build(self, source_dir: Path, store_path: Path) -> dict:
        """读取纳入法规 → 切块打标签 → embedding → 存库 → 落缓存。返回建库统计。"""
        store = EmbeddingStore()
        per_source: list[dict] = []
        all_chunks: list[Chunk] = []

        for src in REGULATION_SOURCES:
            path = source_dir / src["filename"]
            if not path.exists():
                log.warning("法规文件缺失,跳过: %s", path)
                per_source.append({"standard": src["standard"], "chunks": 0, "status": "MISSING"})
                continue
            try:
                text, _ = self.parser.parse(path)
            except Exception as exc:
                log.warning("法规解析失败 %s: %s", src["standard"], exc)
                per_source.append({"standard": src["standard"], "chunks": 0, "status": "PARSE_FAILED"})
                continue

            chunks = self.chunker.chunk(
                source_id=src["standard"],
                source_type="regulation",
                text=text,
                metadata={
                    "standard": src["standard"],
                    "regulation": src["regulation"],
                    "language": src["language"],
                    "source_file": src["filename"],
                },
            )
            all_chunks.extend(chunks)
            per_source.append({"standard": src["standard"], "chunks": len(chunks), "status": "OK"})
            log.info("法规切块 %s: %d 片段", src["standard"], len(chunks))

        if all_chunks:
            # 批量 embedding(建库时算一次),再存进库,记录用的 embedder 名。
            vectors = self.embedder.embed_batch([c.text for c in all_chunks])
            store.add(all_chunks, vectors, embedder_name=getattr(self.embedder, "name", ""))
            store.save(store_path)

        return {
            "embedder": getattr(self.embedder, "name", "unknown"),
            "total_chunks": len(all_chunks),
            "sources": per_source,
            "store_path": str(store_path),
        }
