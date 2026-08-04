from __future__ import annotations

from app.contracts.rag import RagSearchRequest
from app.retrieval.search_projection import OpenSearchProjection
from app.retrieval.vector_retriever import HashEmbeddingRetriever


class SearchResponse:
    def json(self) -> dict:
        return {
            "hits": {
                "total": {"value": 1},
                "hits": [
                    {
                        "_score": 1.5,
                        "_source": {
                            "source_id": "doc-1",
                            "source_type": "policy",
                            "text": "approved evidence",
                            "metadata": {"page": 2},
                        },
                    }
                ],
            }
        }


def test_search_pushes_tenant_and_user_acl_into_index_query() -> None:
    projection = object.__new__(OpenSearchProjection)
    projection.alias = "agent-knowledge-current"
    projection.version = "v7"
    projection.embedder = HashEmbeddingRetriever(8)
    captured: dict = {}

    def request(method: str, path: str, **kwargs):
        captured.update(method=method, path=path, body=kwargs["json"])
        return SearchResponse()

    projection._request = request
    result = projection.search(
        RagSearchRequest(
            query="batch release",
            tenant_id="tenant-a",
            user_id="operator-a",
            top_k=4,
        )
    )

    filters = captured["body"]["query"]["script_score"]["query"]["bool"]["filter"]
    assert {"term": {"tenant_id": "tenant-a"}} in filters
    assert "operator-a" in str(filters)
    assert captured["path"] == "agent-knowledge-current/_search"
    assert result.index_version == "v7"
    assert result.evidence[0].source_id == "doc-1"
