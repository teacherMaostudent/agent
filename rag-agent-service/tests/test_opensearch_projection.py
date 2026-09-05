from __future__ import annotations

from app.contracts.rag import RagSearchRequest
from app.retrieval.embedder import HashEmbedder
from app.retrieval.search_projection import OpenSearchProjection


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
    projection.embedder = HashEmbedder(8)
    projection.embedding_contract = projection.embedder.contract
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

    # The last request is the native OpenSearch dense channel.  Its ACL
    # filter stays inside the k-NN clause, so unauthorized chunks never enter
    # vector ranking before the two channels are fused with RRF.
    filters = captured["body"]["query"]["knn"]["embedding"]["filter"]["bool"]["filter"]
    assert {"term": {"tenant_id": "tenant-a"}} in filters
    assert "operator-a" in str(filters)
    assert "knowledge_status" in str(filters)
    assert "valid_until" in str(filters)
    assert captured["path"] == "agent-knowledge-current/_search"
    assert result.index_version == "v7"
    assert result.embedding_contract_id == projection.embedding_contract.contract_id
    assert result.evidence == []
    assert result.candidates[0].source_id == "doc-1"
    assert result.candidates[0].metadata["fusion"] == "RRF"


def test_superseded_wiki_page_is_deactivated_inside_tenant_boundary() -> None:
    projection = object.__new__(OpenSearchProjection)
    projection.index = "agent-knowledge-v8"
    captured: dict = {}

    class Response:
        pass

    def request(method: str, path: str, **kwargs):
        captured.update(method=method, path=path, body=kwargs["json"])
        return Response()

    projection._request = request
    projection.mark_wiki_superseded("tenant-a", "wiki-old")
    assert captured["method"] == "POST"
    assert "_update_by_query" in captured["path"]
    assert {"term": {"tenant_id": "tenant-a"}} in captured["body"]["query"]["bool"]["filter"]
    assert {"term": {"wiki_page_id": "wiki-old"}} in captured["body"]["query"]["bool"]["filter"]
