"""인제스션 계약 테스트 (Phase 2-3).

핵심: 거버넌스 통과 전 색인 금지 · 청크 id 규약이 검색 dedup 과 쌍을 이룰 것 ·
배치 상한 준수 · 부분 실패를 숨기지 않을 것.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.ask.search import parent_doc_id
from cerno.auth import Identity
from cerno.circuit import breaker
from cerno.clients.ai import AiClient, KeyResolver
from cerno.library.chunk import chunk_id, chunk_text
from cerno.library.ingest import BATCH_LIMIT, build_entries, ingest_document
from cerno.library.manifest import GovernanceError, Manifest, Source

ME = Identity("12345", "clinician")
GROUPS = ["cardio-dept", "research-a"]


def ai(handler) -> AiClient:
    return AiClient(base_url="https://ai.example",
                    keys=KeyResolver(mapping={"12345": "sk-a"}),
                    transport=httpx.MockTransport(handler))


def m(**kw) -> Manifest:
    base = dict(doc_id="doc-1", title="제목", visibility="private",
                owner_clinician_id="12345", phi_reviewed=True)
    base.update(kw)
    return Manifest(**base)


@pytest.fixture(autouse=True)
def _reset():
    breaker("ai").record_success()
    yield
    breaker("ai").record_success()


# ── 청킹 ────────────────────────────────────────────────────

def test_short_text_is_one_chunk():
    assert chunk_text("짧은 문서입니다.") == ["짧은 문서입니다."]


def test_empty_text_yields_nothing():
    assert chunk_text("   ") == []


def test_long_text_splits_under_limit():
    text = "\n\n".join(f"문단 {i}. " + "가" * 200 for i in range(10))
    chunks = chunk_text(text, max_chars=500, overlap=50)
    assert len(chunks) > 1
    # 겹침 때문에 상한을 소폭 넘을 수 있으나 크게 벗어나면 안 된다
    assert all(len(c) <= 500 + 50 for c in chunks)


def test_oversized_sentence_is_kept_whole():
    """문장 중간을 자르면 금기·용량 단서가 잘려 의미가 뒤집힐 수 있다."""
    long_sentence = "이 약은 " + "매우 " * 300 + "위험하므로 임부에게 금기이다."
    chunks = chunk_text(long_sentence, max_chars=100)
    assert any("금기이다" in c for c in chunks)


# ── 청크 id 규약 ↔ 검색 dedup (쌍을 이뤄야 한다) ────────────

def test_chunk_id_roundtrips_through_parent_doc_id():
    assert parent_doc_id(chunk_id("doc-1", 3)) == "doc-1"


def test_group_expanded_chunk_id_also_roundtrips():
    """벗기지 않으면 같은 문서가 그룹 수만큼 중복 인용된다."""
    assert parent_doc_id(chunk_id("doc-1", 3, group="cardio-dept")) == "doc-1"


# ── 거버넌스가 색인보다 먼저다 ──────────────────────────────

@pytest.mark.asyncio
async def test_governance_violation_blocks_before_any_indexing():
    called = {"n": 0}

    def h(req):
        called["n"] += 1
        return httpx.Response(200, json={"count": 1})

    with pytest.raises(GovernanceError):
        await ingest_document(m(phi_reviewed=False), "본문", ident=ME,
                              uploader_groups=GROUPS, ai=ai(h))
    assert called["n"] == 0, "검증 실패인데 색인 요청이 나갔다"


@pytest.mark.asyncio
async def test_cannot_ingest_under_someone_elses_name():
    def h(req):
        return httpx.Response(200, json={"count": 1})

    with pytest.raises(GovernanceError):
        await ingest_document(m(owner_clinician_id="99999"), "본문", ident=ME,
                              uploader_groups=GROUPS, ai=ai(h))


# ── 컬렉션 라우팅 ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_visibility_routes_to_matching_collection():
    seen = []

    def h(req):
        import json as _j
        seen.append(_j.loads(req.content)["collection"])
        return httpx.Response(200, json={"count": 1})

    for vis, col in (("private", "cerno_priv"), ("org", "cerno_org")):
        kw = {"visibility": vis}
        if vis == "org":
            kw |= {"license_approved": True, "source": Source(doi="10.1/x")}
        await ingest_document(m(**kw), "본문", ident=ME, uploader_groups=GROUPS, ai=ai(h))
    assert seen == ["cerno_priv", "cerno_org"]


# ── 다중 그룹 전개 ──────────────────────────────────────────

def test_multi_group_doc_indexed_once_per_group():
    """Chroma 메타는 스칼라만 허용해 group 값을 하나만 담을 수 있다."""
    entries = build_entries(m(visibility="group", groups=GROUPS), "짧은 본문")
    groups = {e["metadata"]["group"] for e in entries}
    assert groups == set(GROUPS)
    assert len(entries) == 2


def test_multi_group_entries_collapse_at_search_time():
    entries = build_entries(m(visibility="group", groups=GROUPS), "짧은 본문")
    assert {parent_doc_id(e["id"]) for e in entries} == {"doc-1"}


def test_single_group_doc_has_no_grp_suffix_collision():
    entries = build_entries(m(visibility="group", groups=["cardio-dept"]), "짧은 본문")
    assert entries[0]["metadata"]["group"] == "cardio-dept"


# ── 배치 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_batches_respect_500_limit():
    sizes = []

    def h(req):
        import json as _j
        sizes.append(len(_j.loads(req.content)["documents"]))
        return httpx.Response(200, json={"count": 1})

    # 청크가 500을 넘도록 긴 본문
    text = "\n\n".join(f"문단 {i}. " + "가" * 400 for i in range(700))
    r = await ingest_document(m(), text, ident=ME, uploader_groups=GROUPS, ai=ai(h))
    assert all(s <= BATCH_LIMIT for s in sizes)
    assert r.requests == len(sizes)
    assert len(sizes) > 1, "배치가 쪼개지지 않았다"


@pytest.mark.asyncio
async def test_metadata_carries_domain_tags_as_scalars():
    seen = {}

    def h(req):
        import json as _j
        seen.update(_j.loads(req.content)["documents"][0]["metadata"])
        return httpx.Response(200, json={"count": 1})

    await ingest_document(m(modalities=["ct", "mri"], icd_codes=["I21"], specialty="cardiology"),
                          "본문", ident=ME, uploader_groups=GROUPS, ai=ai(h))
    assert seen["modalities"] == "ct|mri"
    assert seen["specialty"] == "cardiology"
    assert all(isinstance(v, (str, int, float, bool)) for v in seen.values())


# ── 실패를 숨기지 않는다 ────────────────────────────────────

@pytest.mark.asyncio
async def test_partial_failure_is_reported_not_swallowed():
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] > 1 else httpx.Response(200, json={"count": 1})

    text = "\n\n".join(f"문단 {i}. " + "가" * 400 for i in range(700))
    r = await ingest_document(m(), text, ident=ME, uploader_groups=GROUPS, ai=ai(h))
    assert r.ok is False
    assert r.errors and "실패" in r.errors[0]


@pytest.mark.asyncio
async def test_empty_body_reported():
    def h(req):
        return httpx.Response(200, json={"count": 1})

    r = await ingest_document(m(), "   ", ident=ME, uploader_groups=GROUPS, ai=ai(h))
    assert r.ok is False
    assert "비어" in r.errors[0]


@pytest.mark.asyncio
async def test_429_is_retried_patiently_then_succeeds(monkeypatch):
    """인제스션은 사용자 대기가 없다 — 질의 경로처럼 빨리 포기하지 않는다."""
    import cerno.library.ingest as ing
    monkeypatch.setattr(ing.asyncio, "sleep", lambda *_: _noop())
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(429, json={"retry_after": 0})
        return httpx.Response(200, json={"count": 1})

    r = await ingest_document(m(), "본문", ident=ME, uploader_groups=GROUPS, ai=ai(h))
    assert r.ok, r.errors
    assert calls["n"] > 3


async def _noop():
    return None


def test_chunks_carry_source_doc_id_for_cascade_delete():
    """AI 서버 delete_document 가 이 키로 청크를 동반 삭제한다(v2.124.39).
    없으면 부모 id 삭제가 ok:true 를 반환하면서 조용히 무효가 된다 — 실제로 겪었다."""
    entries = build_entries(m(), "본문")
    assert all(e["metadata"]["source_doc_id"] == "doc-1" for e in entries)


def test_source_doc_id_survives_group_expansion():
    entries = build_entries(m(visibility="group", groups=GROUPS), "본문")
    assert {e["metadata"]["source_doc_id"] for e in entries} == {"doc-1"}
