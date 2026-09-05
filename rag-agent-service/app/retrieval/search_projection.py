"""Disposable OpenSearch projection guarded by tenant and user ACL filters.

The projection accelerates retrieval only.  Source documents remain in the
authoritative repository/object store so an index can be rebuilt or versioned
without changing published knowledge history.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import Any

import httpx
from app.contracts.rag import RagSearchRequest, RagSearchResponse
from app.domain.models import Chunk, Document, RetrievalCandidate, RetrievalChannel
from app.retrieval.embedder import Embedder, build_embedder
from app.retrieval.providers import RetrievalCapabilities


class NullSearchProjection:
    def index_document(self, document: Document, chunks: list[Chunk]) -> None:
        """关闭外部索引时显式丢弃投影更新；权威文档仍已由摄取链路保存。"""
        del document, chunks

    @staticmethod
    def mark_wiki_superseded(tenant_id: str, page_id: str) -> None:
        """Local projection has no external index to mutate."""
        del tenant_id, page_id

    @staticmethod
    def update_source_status(tenant_id: str, source_id: str, status: str) -> int:
        """Local mode has no projection; source status remains in the repository truth."""
        del tenant_id, source_id, status
        return 0


class OpenSearchProjection:
    """Rebuildable OpenSearch projection; PostgreSQL/S3 remain authoritative."""

    capabilities = RetrievalCapabilities(dense=True, lexical=True, scalar_acl_filter=True)

    def __init__(self, settings, *, embedder: Embedder) -> None:
        """绑定索引与嵌入契约；同一别名不得混入无法比较的向量空间。"""
        self.url = settings.opensearch_url.rstrip("/")
        self.alias = settings.opensearch_index_alias
        self.version = settings.opensearch_index_version
        self.index = f"{self.alias}-{self.version}"
        self.embedding_contract = embedder.contract
        self.dim = self.embedding_contract.dimension
        self.auth = (
            (settings.opensearch_username, settings.opensearch_password)
            if settings.opensearch_username
            else None
        )
        self.embedder = embedder
        self._ensure_index()

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """统一执行 OpenSearch 请求；非成功状态立即抛出，交给摄取任务重试。"""
        response = httpx.request(
            method,
            f"{self.url}/{path.lstrip('/')}",
            auth=self.auth,
            timeout=30,
            **kwargs,
        )
        if response.status_code not in {200, 201}:
            response.raise_for_status()
        return response

    def _ensure_index(self) -> None:
        """创建版本化索引及严格映射，随后确保公开别名至少指向一个完整版本。"""
        exists = httpx.head(f"{self.url}/{self.index}", auth=self.auth, timeout=10)
        if exists.status_code == 404:
            self._request(
                "PUT",
                self.index,
                json={
                    "settings": {"index.knn": True},
                    "mappings": {
                        "_meta": {
                            "embedding_contract_id": self.embedding_contract.contract_id,
                            "embedding_contract": self.embedding_contract.model_dump(mode="json"),
                        },
                        "dynamic": "strict",
                        "properties": {
                            "chunk_id": {"type": "keyword"},
                            "document_id": {"type": "keyword"},
                            "document_version": {"type": "keyword"},
                            "source_id": {"type": "keyword"},
                            "source_type": {"type": "keyword"},
                            "tenant_id": {"type": "keyword"},
                            "allowed_users": {"type": "keyword"},
                            "text": {"type": "text"},
                            "embedding": {
                                "type": "knn_vector",
                                "dimension": self.dim,
                                "method": {"name": "hnsw", "space_type": "cosinesimil"},
                            },
                            "embedding_contract_id": {"type": "keyword"},
                            "knowledge_status": {"type": "keyword"},
                            "source_status": {"type": "keyword"},
                            "valid_until": {"type": "date"},
                            "wiki_page_id": {"type": "keyword"},
                            "metadata": {"type": "object", "enabled": False},
                        },
                    },
                },
            )
        else:
            # A reused index version must prove it was created for this exact
            # vector contract; filtering mismatched documents after the fact is
            # not a safe replacement for rebuilding the index.
            mapping = self._request("GET", f"{self.index}/_mapping").json()
            existing_mapping = mapping.get(self.index, {}).get("mappings", {})
            properties = existing_mapping.get("properties", {})
            dimension = properties.get("embedding", {}).get("dimension")
            if dimension != self.dim:
                raise ValueError("existing OpenSearch index dimension does not match embedding contract")
            if existing_mapping.get("_meta", {}).get("embedding_contract_id") != self.embedding_contract.contract_id:
                raise ValueError("existing OpenSearch index contract does not match active embedding provider")
            lifecycle_fields = {
                "knowledge_status",
                "source_status",
                "valid_until",
                "wiki_page_id",
            }
            if not lifecycle_fields.issubset(properties):
                raise ValueError(
                    "existing OpenSearch index lacks Wiki lifecycle fields; publish a new index version"
                )
        alias = httpx.head(f"{self.url}/_alias/{self.alias}", auth=self.auth, timeout=10)
        if alias.status_code == 404:
            self.publish()

    def publish(self) -> None:
        # Alias swapping gives readers an all-or-nothing index version change;
        # they never observe a partially rebuilt index as the active corpus.
        """原子交换别名到当前版本，读请求不会观察到半重建的索引。"""
        current = httpx.get(f"{self.url}/_alias/{self.alias}", auth=self.auth, timeout=10)
        actions: list[dict[str, Any]] = []
        if current.status_code == 200:
            actions.extend(
                {"remove": {"index": index, "alias": self.alias}} for index in current.json()
            )
        actions.append({"add": {"index": self.index, "alias": self.alias}})
        self._request("POST", "_aliases", json={"actions": actions})

    def index_document(self, document: Document, chunks: list[Chunk]) -> None:
        """把已持久化文档投影为带租户和用户 ACL 的索引记录。

        缺少 tenant_id 的文档直接拒绝，确保无法生成默认公开的知识证据。
        """
        tenant_id = str(document.metadata.get("tenant_id", ""))
        if not tenant_id:
            raise ValueError("indexed documents require tenant_id")
        allowed_users = document.metadata.get("allowed_users") or []
        for chunk in chunks:
            identifier = hashlib.sha256(f"{tenant_id}:{chunk.chunk_id}".encode()).hexdigest()
            # Persist the exact text digest with the indexed projection. The
            # verifier can later reject a corrupted/stale index record instead
            # of presenting altered content as authoritative evidence.
            stored_metadata = {
                **chunk.metadata,
                "content_sha256": hashlib.sha256(chunk.text.encode("utf-8")).hexdigest(),
                "document_id": document.document_id,
                "document_version": str(document.metadata.get("document_version", "")),
            }
            self._request(
                "PUT",
                f"{self.index}/_doc/{identifier}",
                json={
                    "chunk_id": chunk.chunk_id,
                    "document_id": document.document_id,
                    "document_version": str(document.metadata.get("document_version", "")),
                    # Prefer the upstream source identity when present. The
                    # document/chunk identities remain separate provenance
                    # fields, while source lifecycle events can now deactivate
                    # every document imported from that upstream source.
                    "source_id": document.metadata.get("source_id", chunk.source_id),
                    "source_type": chunk.source_type,
                    "tenant_id": tenant_id,
                    "allowed_users": allowed_users,
                    "text": chunk.text,
                    "embedding": self.embedder._embed(chunk.text),
                    "embedding_contract_id": self.embedding_contract.contract_id,
                    "knowledge_status": document.metadata.get("knowledge_status", "active"),
                    # Source revocation is separate from document lifecycle:
                    # a still-active document may originate from a source that
                    # was later quarantined and must no longer reach Context.
                    "source_status": document.metadata.get("source_status", "active"),
                    "valid_until": document.metadata.get("valid_until"),
                    "wiki_page_id": document.metadata.get("page_id"),
                    "metadata": stored_metadata,
                },
            )

    def mark_wiki_superseded(self, tenant_id: str, page_id: str) -> None:
        """Deactivate every indexed chunk of a superseded Wiki page inside the ACL boundary."""
        self._request(
            "POST",
            f"{self.index}/_update_by_query?conflicts=proceed&refresh=true",
            json={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                            {"term": {"wiki_page_id": page_id}},
                        ]
                    }
                },
                "script": {"source": "ctx._source.knowledge_status = 'superseded'"},
            },
        )

    def update_source_status(self, tenant_id: str, source_id: str, status: str) -> int:
        """Propagate an upstream source revocation/quarantine to every indexed chunk.

        The tenant filter is mandatory.  This operation does not delete the
        source text, because retained records may be required for audit or
        legal hold; EvidenceVerifier simply stops projecting the source.
        """

        response = self._request(
            "POST",
            f"{self.index}/_update_by_query?conflicts=proceed&refresh=true",
            json={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"tenant_id": tenant_id}},
                            {"term": {"source_id": source_id}},
                        ]
                    }
                },
                "script": {"source": "ctx._source.source_status = params.status", "params": {"status": status}},
            },
        ).json()
        return int(response.get("updated", 0))

    def search(self, request: RagSearchRequest) -> RagSearchResponse:
        # ACL constraints are filters, not post-processing.  Unauthorized
        # chunks must not influence scores or candidate counts at all.
        """在索引查询内施加 ACL 过滤后做词法/向量混合检索，禁止事后过滤。"""
        if request.index_version and request.index_version != self.version:
            raise ValueError("requested RAG index version is not active on this query service")
        if (
            request.embedding_contract_id
            and request.embedding_contract_id != self.embedding_contract.contract_id
        ):
            raise ValueError("requested embedding contract does not match active RAG index")
        acl_filter = [
            {"term": {"tenant_id": request.tenant_id}},
            {"term": {"embedding_contract_id": self.embedding_contract.contract_id}},
            {
                "bool": {
                    "should": [
                        {"term": {"allowed_users": request.user_id}},
                        {"bool": {"must_not": {"exists": {"field": "allowed_users"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            {
                "bool": {
                    "should": [
                        {"term": {"knowledge_status": "active"}},
                        {"bool": {"must_not": {"exists": {"field": "knowledge_status"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            {
                "bool": {
                    "should": [
                        {"range": {"valid_until": {"gt": datetime.now(UTC).isoformat()}}},
                        {"bool": {"must_not": {"exists": {"field": "valid_until"}}}},
                    ],
                    "minimum_should_match": 1,
                }
            },
        ]
        # Run lexical and dense recall independently.  Their scores are not
        # comparable, so fusion happens by rank below rather than by an opaque
        # weighted script_score expression.
        lexical = self._search_channel(
            request,
            acl_filter,
            RetrievalChannel.LEXICAL,
            {"bool": {"filter": acl_filter, "must": [{"match": {"text": request.query}}]}},
        )
        dense = self._search_channel(
            request,
            acl_filter,
            RetrievalChannel.DENSE,
            {
                # ``script_score`` + Elasticsearch's ``cosineSimilarity`` is
                # not a portable OpenSearch k-NN query and is rejected by the
                # OpenSearch 3 k-NN plugin.  Keep the ACL filters *inside* the
                # native k-NN clause so filtering happens before candidates
                # are exposed or fused, rather than falling back to unsafe
                # post-filtering in Python.
                "knn": {
                    "embedding": {
                        "vector": self.embedder.embed(request.query),
                        "k": request.top_k,
                        "filter": {"bool": {"filter": acl_filter}},
                    }
                }
            },
        )
        candidates = self._rrf(lexical, dense)
        return RagSearchResponse(
            query=request.query,
            candidate_count=len(candidates),
            index_version=self.version,
            embedding_contract_id=self.embedding_contract.contract_id,
            candidates=candidates,
        )

    def retrieve(self, request: RagSearchRequest) -> list[RetrievalCandidate]:
        """Provider-compatible candidate-only entry point for future coordinators."""

        return self.search(request).candidates

    def _search_channel(
        self,
        request: RagSearchRequest,
        acl_filter: list[dict[str, Any]],
        channel: RetrievalChannel,
        query: dict[str, Any],
    ) -> list[RetrievalCandidate]:
        """Read one ACL-filtered channel and preserve backend scores as diagnostic lineage."""

        del acl_filter  # Filters are embedded in the query and retained for explicit call-site audit.
        response = self._request(
            "POST", f"{self.alias}/_search", json={"size": request.top_k, "query": query}
        ).json()
        hits = response.get("hits", {}).get("hits", [])
        candidates: list[RetrievalCandidate] = []
        for rank, item in enumerate(hits, start=1):
            source = item["_source"]
            metadata = {
                **source.get("metadata", {}),
                "tenant_id": source.get("tenant_id", ""),
                "allowed_users": source.get("allowed_users") or [],
                "knowledge_status": source.get("knowledge_status", "active"),
                "source_status": source.get("source_status", "active"),
                "valid_until": source.get("valid_until"),
                "backend_score": float(item.get("_score", 0.0)),
                "backend": "opensearch",
            }
            candidates.append(
                RetrievalCandidate(
                    chunk_id=str(source.get("chunk_id", "")),
                    document_id=str(source.get("document_id", "")),
                    document_version=str(source.get("document_version", "")),
                    source_id=source["source_id"],
                    source_type=source["source_type"],
                    text=source["text"],
                    score=float(item.get("_score", 0.0)),
                    channel=channel,
                    rank=rank,
                    metadata=metadata,
                )
            )
        return candidates

    @staticmethod
    def _rrf(
        lexical: list[RetrievalCandidate], dense: list[RetrievalCandidate], *, k: int = 60
    ) -> list[RetrievalCandidate]:
        """Deduplicate by immutable chunk identity and fuse independent ranks with RRF."""

        merged: dict[str, RetrievalCandidate] = {}
        channels: dict[str, set[str]] = {}
        for hits in (lexical, dense):
            for rank, item in enumerate(hits, start=1):
                key = item.chunk_id or f"{item.source_id}:{item.metadata.get('start', 0)}"
                if key not in merged:
                    merged[key] = item.model_copy(update={"score": 1.0 / (k + rank)})
                    channels[key] = {item.channel.value}
                else:
                    merged[key].score += 1.0 / (k + rank)
                    channels[key].add(item.channel.value)
        ordered = sorted(merged.items(), key=lambda pair: (-pair[1].score, pair[0]))
        return [
            item.model_copy(
                update={
                    "rank": rank,
                    "channel": RetrievalChannel.HYBRID,
                    "metadata": {
                        **item.metadata,
                        "retrieval_channels": sorted(channels[key]),
                        "fusion": "RRF",
                        "fusion_revision": "rrf/v1",
                    },
                }
            )
            for rank, (key, item) in enumerate(ordered, start=1)
        ]


def build_search_projection(settings, *, embedder: Embedder | None = None):
    """按发布配置创建可重建检索投影；本地模式返回无操作实现。"""
    if settings.search_backend == "opensearch":
        return OpenSearchProjection(settings, embedder=embedder or build_embedder(settings))
    return NullSearchProjection()
