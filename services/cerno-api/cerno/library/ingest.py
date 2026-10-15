"""인제스션 — 매니페스트 검증 → 청킹 → 배치 색인.

01_구현계획.md Phase 2-3.

지키는 것:
  1. **거버넌스 통과 전에는 한 글자도 색인하지 않는다.** 검증을 색인 도중에 하면 일부만
     들어간 상태로 실패해 정합성이 깨진다.
  2. **배치 필수** — 단건 1.47s/건 vs 배치 ~12ms/건(약 100배). 상한 500건.
  3. **rate limit 은 요청 수 기준**(키당 120/min, DELETE 포함). 배치 1회 = 1요청.
  4. 인제스션은 사용자 대기가 없는 배치 작업이므로 429 에 **길게 기다린다** —
     질의 경로처럼 빨리 포기하지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

from ..auth import Identity
from ..clients.ai import AiBusy, AiClient, AiUnavailable
from .chunk import DEFAULT_MAX_CHARS, DEFAULT_OVERLAP, chunk_id, chunk_text
from .manifest import Manifest, validate_or_raise

log = logging.getLogger("cerno.ingest")

BATCH_LIMIT = 500
"""AI 서버 상한. 초과 시 413."""

COLLECTION_BY_VISIBILITY = {
    "private": "cerno_priv",
    "group": "cerno_grp",
    "org": "cerno_org",
}

_INGEST_MAX_RETRY = 6
_INGEST_MAX_BACKOFF = 60.0


@dataclass
class IngestResult:
    doc_id: str
    collection: str
    chunks: int = 0
    requests: int = 0
    groups_expanded: int = 1
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def build_entries(
    m: Manifest,
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[dict]:
    """매니페스트 + 본문 → `/api/rag/index` 배치 항목.

    **다중 그룹 전개.** ChromaDB 메타데이터는 스칼라만 허용해 `group` 에 값을 하나만
    담을 수 있고, `$in` 필터는 스칼라 동등 비교라 구분자 문자열을 매칭하지 못한다.
    따라서 그룹이 여럿이면 **그룹마다 별도 색인**한다. 검색 시에는 `parent_doc_id` 가
    `__grp_` 접미를 벗겨 하나로 접으므로 중복 인용되지 않는다.
    """
    chunks = chunk_text(text, max_chars=max_chars, overlap=overlap)
    if not chunks:
        return []

    base_meta = m.metadata()
    targets: list[str | None] = list(m.groups) if m.visibility == "group" and m.groups else [None]

    entries: list[dict] = []
    for g in targets:
        meta = dict(base_meta)
        if g is not None:
            meta["group"] = g
        for i, c in enumerate(chunks):
            entries.append({
                "id": chunk_id(m.doc_id, i, group=g),
                "text": c,
                "metadata": {
                    **meta, "chunk": i, "chunks": len(chunks),
                    # AI 서버 `delete_document` 는 이 키로 청크를 동반 삭제한다
                    # (v2.124.39). 없으면 **부모 id 삭제가 조용히 무효**가 되어 벡터가
                    # 영구 잔존한다 — 실제로 겪었다(2026-07-30 정리 중 ok:true 인데
                    # count 불변). 우리 delete_document 는 청크 id 를 명시 삭제하므로
                    # 이건 안전망이다: 등록부를 잃었을 때 부모 id 만으로 지울 수 있다.
                    "source_doc_id": m.doc_id,
                },
            })
    return entries


async def _index_with_backoff(
    ai: AiClient, docs: list[dict], *, clinician_id: str, collection: str
) -> None:
    """429 에 인내심 있게 재시도. 배치 작업이라 사용자가 기다리지 않는다."""
    delay = 2.0
    for attempt in range(_INGEST_MAX_RETRY):
        try:
            await ai.index_batch(docs, clinician_id=clinician_id, collection=collection)
            return
        except AiBusy as e:
            wait = min(max(e.retry_after, delay), _INGEST_MAX_BACKOFF)
            if attempt + 1 >= _INGEST_MAX_RETRY:
                raise
            log.info("색인 429 → %.0fs 대기(%d/%d)", wait, attempt + 1, _INGEST_MAX_RETRY)
            await asyncio.sleep(wait)
            delay = min(delay * 2, _INGEST_MAX_BACKOFF)


async def ingest_document(
    m: Manifest,
    text: str,
    *,
    ident: Identity,
    uploader_groups: list[str],
    ai: AiClient,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> IngestResult:
    """단일 문서 적재. 거버넌스 위반 시 `GovernanceError` 로 **색인 전에** 중단한다.

    `ident` 는 검증된 신원이다 — 매니페스트의 owner 를 신뢰하지 않는다.
    """
    validate_or_raise(m, uploader_id=ident.clinician_id, uploader_groups=uploader_groups)

    collection = COLLECTION_BY_VISIBILITY[m.visibility]
    entries = build_entries(m, text, max_chars=max_chars, overlap=overlap)
    n_groups = max(1, len(m.groups)) if m.visibility == "group" else 1
    res = IngestResult(doc_id=m.doc_id, collection=collection,
                       chunks=len(entries) // n_groups if n_groups else 0,
                       groups_expanded=n_groups)
    if not entries:
        res.errors.append("본문이 비어 색인할 청크가 없습니다")
        return res

    for i in range(0, len(entries), BATCH_LIMIT):
        batch = entries[i:i + BATCH_LIMIT]
        try:
            await _index_with_backoff(ai, batch, clinician_id=ident.clinician_id,
                                      collection=collection)
            res.requests += 1
        except (AiBusy, AiUnavailable) as e:
            # 부분 적재 상태를 숨기지 않는다 — 재적재는 upsert 멱등이라 안전하다.
            res.errors.append(f"배치 {i // BATCH_LIMIT + 1} 실패: {e}")
            log.warning("색인 중단 doc=%s 배치=%d: %s", m.doc_id, i // BATCH_LIMIT + 1, e)
            break

    return res


async def delete_document(
    m: Manifest, *, ident: Identity, ai: AiClient, chunk_count: int
) -> int:
    """문서 삭제. 청크·그룹 전개분을 전부 지운다.

    ⚠️ **DELETE 도 키당 120/min 에 포함**된다. 청크가 많은 문서는 요청 수가 그만큼 든다.
    AI 서버 삭제는 멱등이라 없는 id 도 ok 다.
    """
    targets = list(m.groups) if m.visibility == "group" and m.groups else [None]
    collection = COLLECTION_BY_VISIBILITY[m.visibility]
    deleted = 0
    for g in targets:
        for i in range(chunk_count):
            cid = chunk_id(m.doc_id, i, group=g)
            try:
                await ai.delete_doc(cid, clinician_id=ident.clinician_id,
                                    collection=collection)
                deleted += 1
            except (AiBusy, AiUnavailable) as e:
                log.warning("삭제 실패 %s: %s", cid, e)
    return deleted
