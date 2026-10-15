"""Cerno Library API — 문서 검증·적재·목록·삭제.

포털(cerno-web)이 소비한다. 계약의 핵심은 **검증과 적재의 분리**다 —
포털이 적재 전에 위반 사유를 전부 보여줄 수 있어야 한다.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..auth import Identity
from ..clients.ai import AiBusy, AiUnavailable
from ..library.chunk import chunk_text
from ..library.ingest import COLLECTION_BY_VISIBILITY, ingest_document
from ..library.manifest import GovernanceError, Manifest, parse, validate
from ..obs import metrics
from .deps import get_ai, get_store, groups_of, require_identity

log = logging.getLogger("cerno.api.library")

router = APIRouter(prefix="/library", tags=["library"])


class DocumentIn(BaseModel):
    manifest: dict = Field(..., description="매니페스트(YAML 로드 결과 형태)")
    text: str = Field("", description="본문. 저장하지 않고 청킹 후 AI 서버로만 흘린다")


class ValidationOut(BaseModel):
    ok: bool
    violations: list[str] = []
    chunk_preview: int = 0
    collection: str = ""


def _parsed(raw: dict, ident: Identity) -> Manifest:
    """owner 를 **검증된 신원으로 덮어쓴다.**

    포털 폼이 owner 를 보내든 말든 신원이 진실이다. 매니페스트 값을 신뢰해 비교만 하면
    위조 여지가 남는다.
    """
    try:
        m = parse({**raw, "owner_clinician_id": ident.clinician_id})
    except GovernanceError as e:
        raise HTTPException(status_code=400, detail={"violations": e.violations}) from None
    return m


@router.post("/validate", response_model=ValidationOut)
def validate_document(body: DocumentIn, ident: Identity = Depends(require_identity)):
    """적재하지 않고 검증만. 포털이 위반 사유를 **전량** 보여주기 위한 경로."""
    m = _parsed(body.manifest, ident)
    v = validate(m, uploader_id=ident.clinician_id, uploader_groups=groups_of(ident))
    return ValidationOut(
        ok=not v, violations=v,
        chunk_preview=len(chunk_text(body.text)),
        collection=COLLECTION_BY_VISIBILITY.get(m.visibility, ""),
    )


@router.post("/documents")
async def create_document(body: DocumentIn, ident: Identity = Depends(require_identity)):
    m = _parsed(body.manifest, ident)
    try:
        res = await ingest_document(
            m, body.text, ident=ident, uploader_groups=groups_of(ident), ai=get_ai(),
        )
    except GovernanceError as e:
        # 색인 전에 걸린 것 — 아무것도 들어가지 않았다
        raise HTTPException(status_code=422, detail={"violations": e.violations}) from None
    except AiBusy as e:
        metrics.record_ingest(ok=False, chunks=0)
        raise HTTPException(status_code=503, detail="AI 서버 혼잡",
                            headers={"Retry-After": str(int(e.retry_after))}) from None
    except AiUnavailable:
        metrics.record_ingest(ok=False, chunks=0)
        raise HTTPException(status_code=503, detail="AI 서버를 사용할 수 없습니다") from None

    metrics.record_ingest(ok=res.ok, chunks=res.chunks)
    if not res.ok:
        # 부분 적재를 성공으로 보고하지 않는다. 재적재는 upsert 멱등이라 안전하다.
        raise HTTPException(status_code=502, detail={"errors": res.errors,
                                                     "partial": True})

    get_store().upsert(m, chunk_count=res.chunks)
    return {"doc_id": m.doc_id, "collection": res.collection,
            "chunks": res.chunks, "groups_expanded": res.groups_expanded}


@router.get("/documents")
def list_documents(ident: Identity = Depends(require_identity)):
    """**접근 가능한 문서만.** 검색과 동일한 격리 규칙 — 어긋나면 검색으로는 못 보는
    문서를 포털에서 보게 된다."""
    recs = get_store().list_visible(owner=ident.clinician_id, groups=groups_of(ident))
    return {"documents": [
        {"doc_id": r.doc_id, "title": r.title, "visibility": r.visibility,
         "chunks": r.chunk_count, "groups": r.groups,
         "mine": r.owner == ident.clinician_id, "updated_at": r.updated_at}
        for r in recs
    ]}


@router.delete("/documents/{doc_id}")
async def remove_document(doc_id: str, ident: Identity = Depends(require_identity)):
    """소유자만 삭제한다. 공용 문서라도 남이 지울 수 없다."""
    from ..library.ingest import delete_document as delete_vectors

    store = get_store()
    found = store.get(doc_id)
    if not found:
        return {"deleted": 0, "doc_id": doc_id}      # 멱등
    rec, raw = found
    if rec.owner != ident.clinician_id:
        raise HTTPException(status_code=403, detail="소유자만 삭제할 수 있습니다")

    m = parse(raw)
    n = await delete_vectors(m, ident=ident, ai=get_ai(), chunk_count=rec.chunk_count)
    store.delete(doc_id)
    return {"deleted": n, "doc_id": doc_id}
