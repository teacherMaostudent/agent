"""Knowledge compilation, conflict detection and approval state machine."""

import hashlib
import re
from datetime import UTC, datetime

from platform_infra.schema_registry import SchemaRegistry

from app.models import (
    CandidateStatus,
    CompileRequest,
    KnowledgeLevel,
    KnowledgeSource,
    OutboxEvent,
    RelationType,
    ReviewRequest,
    ReviewResult,
    WikiCandidate,
    WikiPage,
    WikiRelation,
    now,
)
from app.repository import WikiRepository

_SLUG = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")


def canonical_key(title: str) -> str:
    """Create a stable human-readable identity without letting titles become paths."""
    return _SLUG.sub("-", title.strip().lower()).strip("-")[:200]


def digest(*values: str) -> str:
    """Hash ordered semantic fields so confirmed versions can detect content drift."""
    return hashlib.sha256("\n".join(values).encode("utf-8")).hexdigest()


def render_markdown(page: WikiPage) -> str:
    """Render the immutable RAG source; front matter preserves governed provenance."""
    source_ids = ", ".join(item.source_id for item in page.sources)
    relation_lines = "\n".join(
        f"- {item.relation_type.value}: {item.target_page_id} ({item.reason})"
        for item in page.relations
    ) or "- none"
    return (
        f"# {page.title}\n\n"
        f"> Knowledge level: {page.knowledge_level.value}\n"
        f"> Wiki page: {page.page_id} v{page.version}\n"
        f"> Candidate: {page.candidate_id}\n"
        f"> Approved by: {page.approved_by}\n"
        f"> Sources: {source_ids}\n\n"
        f"## Summary\n\n{page.summary}\n\n"
        f"## Content\n\n{page.body}\n\n"
        f"## Governed relations\n\n{relation_lines}\n"
    )


class KnowledgeWikiService:
    """Own candidate compilation, human promotion and lifecycle relation construction."""

    def __init__(
        self, repository: WikiRepository, *, schema_registry: SchemaRegistry
    ) -> None:
        """Bind the authoritative repository; downstream delivery remains the relay's concern."""
        self.repository = repository
        self.schema_registry = schema_registry

    def compile(self, tenant_id: str, user_id: str, request: CompileRequest) -> WikiCandidate:
        """Register model output as a review candidate; compilation never publishes knowledge."""
        candidate = WikiCandidate(
            tenant_id=tenant_id, submitted_by=user_id, root_task_id=request.root_task_id,
            conclusion=request.conclusion, sources=request.sources, drafts=request.drafts,
            compiler_model=request.compiler_model,
            compiler_prompt_version=request.compiler_prompt_version,
        )
        return self.repository.create_candidate(candidate)

    def review(
        self, tenant_id: str, reviewer_id: str, candidate_id: str, request: ReviewRequest
    ) -> ReviewResult:
        """Approve/reject once; approval atomically emits confirmed pages and downstream intents."""
        candidate = self.repository.get_candidate(tenant_id, candidate_id)
        if candidate is None:
            raise KeyError(candidate_id)
        reviewed = candidate.model_copy(
            update={
                "status": (
                    CandidateStatus.APPROVED
                    if request.decision == "approve"
                    else CandidateStatus.REJECTED
                ),
                "reviewer_id": reviewer_id,
                "review_comment": request.comment,
                "reviewed_at": now(),
            }
        )
        if request.decision == "reject":
            pages, events = self.repository.review(
                reviewed, request.expected_status.value, lambda _: ([], [])
            )
            return ReviewResult(candidate=reviewed, pages=pages, outbox_event_ids=[])

        def build(_connection):
            """Construct page versions and relations inside the repository review transaction."""
            pages: list[WikiPage] = []
            events: list[OutboxEvent] = []
            all_pages = self.repository.pages(tenant_id)
            by_id = {item.page_id: item for item in all_pages}
            by_key: dict[str, list[WikiPage]] = {}
            for item in all_pages:
                by_key.setdefault(item.canonical_key, []).append(item)
            for draft in reviewed.drafts:
                key = canonical_key(draft.title)
                previous = sorted(by_key.get(key, []), key=lambda item: item.version, reverse=True)
                content_hash = digest(draft.title, draft.summary, draft.body)
                if previous and previous[0].content_sha256 == content_hash:
                    raise ValueError("identical confirmed Wiki content already exists")
                relations: list[WikiRelation] = []
                for old in previous[:1]:
                    if old.content_sha256 != content_hash:
                        relations.append(WikiRelation(
                            relation_type=RelationType.CONFLICTS_WITH,
                            target_page_id=old.page_id,
                            reason="same canonical topic has different confirmed content",
                        ))
                if draft.supersedes_page_id:
                    target = by_id.get(draft.supersedes_page_id)
                    if target is None:
                        raise ValueError("superseded page does not exist in this tenant")
                    relations.append(WikiRelation(
                        relation_type=RelationType.SUPERSEDES,
                        target_page_id=target.page_id,
                        reason=request.comment,
                    ))
                draft_tags = set(draft.tags)
                for related in all_pages:
                    if draft_tags.intersection(related.tags) and related.canonical_key != key:
                        relations.append(WikiRelation(
                            relation_type=RelationType.LINKS_TO,
                            target_page_id=related.page_id,
                            reason="shared governed tag",
                        ))
                review_source = KnowledgeSource(
                    source_id=f"review:{reviewed.candidate_id}",
                    source_type="review",
                    knowledge_level=KnowledgeLevel.HUMAN_CONFIRMED,
                    content_sha256=digest(reviewer_id, request.comment, reviewed.candidate_id),
                )
                page = WikiPage(
                    tenant_id=tenant_id, canonical_key=key, title=draft.title,
                    page_type=draft.page_type, summary=draft.summary, body=draft.body,
                    tags=sorted(draft_tags), sources=[*reviewed.sources, review_source],
                    relations=relations,
                    content_sha256=content_hash,
                    version=(previous[0].version + 1 if previous else 1),
                    valid_until=draft.valid_until, approved_by=reviewer_id,
                    approval_comment=request.comment, candidate_id=reviewed.candidate_id,
                )
                pages.append(page)
                base = {"page_id": page.page_id, "candidate_id": reviewed.candidate_id,
                        "content_sha256": page.content_sha256, "version": page.version}
                for event_type in (
                    "wiki.page.published", "wiki.rag.reindex.requested",
                    "wiki.evaluation.requested", "wiki.release_gate.requested",
                ):
                    payload = dict(base)
                    if event_type == "wiki.rag.reindex.requested":
                        markdown = render_markdown(page)
                        payload.update({
                            "title": page.title,
                            "markdown": markdown,
                            "content_sha256": hashlib.sha256(markdown.encode("utf-8")).hexdigest(),
                            "approved_by": reviewer_id,
                            "source_ids": [item.source_id for item in page.sources],
                            "valid_until": (
                                page.valid_until.isoformat() if page.valid_until else None
                            ),
                            "supersedes_page_ids": [
                                item.target_page_id
                                for item in page.relations
                                if item.relation_type is RelationType.SUPERSEDES
                            ],
                        })
                    events.append(OutboxEvent(
                        tenant_id=tenant_id, event_type=event_type, payload=payload
                    ))
                    self.schema_registry.validate(
                        "knowledge-wiki-event.v1.json",
                        events[-1].model_dump(mode="json"),
                    )
            return pages, events

        pages, events = self.repository.review(reviewed, request.expected_status.value, build)
        return ReviewResult(
            candidate=reviewed, pages=pages, outbox_event_ids=[item.event_id for item in events]
        )

    def list_pages(self, tenant_id: str) -> list[WikiPage]:
        """Project time-dependent expiry and supersede status without mutating immutable pages."""
        current = datetime.now(UTC)
        pages = self.repository.pages(tenant_id)
        superseded = {
            relation.target_page_id
            for page in pages
            for relation in page.relations
            if relation.relation_type is RelationType.SUPERSEDES
        }
        return [
            item.model_copy(
                update={
                    "status": (
                        "superseded"
                        if item.page_id in superseded
                        else "expired"
                        if item.valid_until and item.valid_until <= current
                        else item.status
                    )
                }
            )
            for item in pages
        ]
