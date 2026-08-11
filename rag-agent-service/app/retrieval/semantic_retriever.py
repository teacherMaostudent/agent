"""语义检索(双库)。

用同一个 embedder 支撑两类检索,呼应"双库"设计:
- 法规库(预建、持久化):给每条要求找对应法规原文,当报告里的"依据引用"。
- 企业文件库(每次审查临时建):判"企业写没写这条要求"(COVERED/MISSING 的本意)。

embedder 与建库同源,保证查询向量和库向量可比。查询只 embed 一次。
"""

import re

from app.domain.models import Chunk, Evidence
from app.ingestion.chunker import TextChunker
from app.retrieval.embedding_store import EmbeddingStore


class SemanticRetriever:
    def __init__(self, embedder, regulation_store: EmbeddingStore) -> None:
        """绑定同源嵌入器与法规向量库，保证查询和索引位于同一向量空间。"""
        self.embedder = embedder
        self.regulation_store = regulation_store
        self._chunker = TextChunker()

    def has_regulation_library(self) -> bool:
        """判断法规索引是否已有可用片段，供上层选择语义检索或空结果路径。"""
        return len(self.regulation_store) > 0

    def search_regulations(
        self,
        query: str,
        top_k: int = 4,
        metadata_filter: dict | None = None,
    ) -> list[Evidence]:
        """在法规库检索对应法规条文(依据引用)。库空时返回空列表。"""
        if len(self.regulation_store) == 0:
            return []
        query_vec = self.embedder.embed(query)
        return self.regulation_store.search(query_vec, top_k=top_k, metadata_filter=metadata_filter)

    def build_document_store(self, document_id: str, text: str) -> EmbeddingStore:
        """把企业文件当场切块建临时库(判企业写没写)。用完即弃,不落地。"""
        chunks = self._chunker.chunk(
            source_id=document_id,
            source_type="enterprise_document",
            text=text,
            metadata={},
        )
        store = EmbeddingStore()
        if chunks:
            vectors = self.embedder.embed_batch([c.text for c in chunks])
            store.add(chunks, vectors)
        return store

    def search_document(
        self,
        query: str,
        document_store: EmbeddingStore,
        top_k: int = 4,
    ) -> list[Evidence]:
        """在企业文件临时库里找与某条要求相关的片段(判覆盖/缺失)。"""
        if len(document_store) == 0:
            return []
        query_vec = self.embedder.embed(query)
        return document_store.search(query_vec, top_k=top_k)

    # Temporary multi-document index: used only for the current operation and never persisted.

    def build_document_set_store(self, files: list[tuple[str, str, str]]) -> EmbeddingStore:
        """把多份企业文件切块建成同一个临时库(跨文档分析用)。

        files: [(document_id, filename, text), ...]。每片打 {document_id, filename}
        标签,后续召回能认出"来自哪份文件"(矛盾检测要用)。零持久化:返回的 store
        是局部对象,函数外无引用即被回收,不落盘、不污染任何持久库。
        """
        all_chunks: list[Chunk] = []
        for document_id, filename, text in files:
            all_chunks.extend(
                self._chunker.chunk(
                    source_id=document_id,
                    source_type="enterprise_document",
                    text=text,
                    metadata={"document_id": document_id, "filename": filename},
                )
            )
        store = EmbeddingStore()
        if all_chunks:
            vectors = self.embedder.embed_batch([c.text for c in all_chunks])
            store.add(all_chunks, vectors, embedder_name=getattr(self.embedder, "name", ""))
        return store

    def search_document_set(
        self,
        query: str,
        document_store: EmbeddingStore,
        per_file_k: int = 3,
    ) -> list[Evidence]:
        """跨整个文件集检索,但每份文件各保底召回 per_file_k 条(而非全局抢位)。

        为什么保底(对应 Q5):若全局 top_k,提及某主题多的文件会占满名额,把
        只提一次的另一份挤掉——而矛盾恰恰需要双方都在场。按 document_id 分桶各取
        前 per_file_k,保证每份文件在每个主题上都有代表片段进入判断,杜绝静默漏检。
        """
        if len(document_store) == 0:
            return []
        query_vec = self.embedder.embed(query)
        # 取足够多再分桶:candidate 数放大,避免分桶前就被截断。
        candidates = document_store.search(query_vec, top_k=len(document_store))
        per_file: dict[str, list[Evidence]] = {}
        for ev in candidates:
            doc_id = ev.metadata.get("document_id", ev.source_id)
            bucket = per_file.setdefault(doc_id, [])
            if len(bucket) < per_file_k:
                bucket.append(ev)
        results: list[Evidence] = []
        for bucket in per_file.values():
            results.extend(bucket)
        return results

    def keyword_scan_document_set(
        self,
        files: list[tuple[str, str, str]],
        terms: list[str],
        window: int = 60,
        max_hits_per_file: int = 3,
    ) -> list[Evidence]:
        """双通道的第二通道:在原文上直接扫关键词/别名,不经向量、不受 top_k 限制。

        为什么要它(对应 Q5/Q6):向量召回会漏——① 主题在长文件里出现多次,
        相似度前几名挤不进的那次被漏(文件内稀释);② 文件用词与主题词不同源
        (如"环境控制级别"vs"洁净度"),向量根本召不回。关键词全文扫是兜底:
        只要某个 term(含别名)在原文出现,就把命中处上下文捞出来,与向量通道并集。

        terms: 主题词 + 其所有别名。命中任一即算。返回 Evidence 结构与向量通道
        同构(带 document_id/filename),方便调用方直接合并、去重。
        """
        results: list[Evidence] = []
        for document_id, filename, text in files:
            flat = "\n".join(line.strip() for line in text.splitlines() if line.strip())
            hits = 0
            seen_spans: set[tuple[int, int]] = set()
            for term in terms:
                if not term or hits >= max_hits_per_file:
                    continue
                for m in re.finditer(re.escape(term), flat):
                    if hits >= max_hits_per_file:
                        break
                    start = max(0, m.start() - window)
                    end = min(len(flat), m.end() + window)
                    # 命中处上下文若与已收的高度重叠,跳过,避免同一句话重复入选。
                    if any(abs(start - s) < window for s, _ in seen_spans):
                        continue
                    seen_spans.add((start, end))
                    results.append(
                        Evidence(
                            source_id=document_id,
                            source_type="enterprise_document",
                            text=flat[start:end],
                            score=1.0,  # 关键词精确命中,给满分与向量分区分
                            metadata={
                                "document_id": document_id,
                                "filename": filename,
                                "channel": "keyword",
                                "matched_term": term,
                            },
                        )
                    )
                    hits += 1
        return results
