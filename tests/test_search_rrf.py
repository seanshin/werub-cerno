"""근거 검색·RRF 병합·게이트 계약 테스트 (01_구현계획.md §3.1·§4).

핵심: 근거가 부족하면 **생성하지 않는다**. 개인 자료는 인용에 내용이 새지 않는다.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.auth import Identity
from cerno.circuit import breaker
from cerno.clients.ai import AiBusy, AiClient, Hit, KeyResolver
from cerno.ask.search import rrf_merge, search_evidence
from cerno.auth.scope import CollectionScope

ME = Identity("12345", "clinician")
KEYS = KeyResolver(mapping={"12345": "sk-a", "67890": "sk-b"}, fallback="sk-shared")


def ai(handler) -> AiClient:
    return AiClient(base_url="https://ai.example", keys=KEYS,
                    transport=httpx.MockTransport(handler))


def result(*rows):
    return {"results": [
        {"id": i, "text": t, "score": s, "metadata": md}
        for i, t, s, md in rows
    ]}


@pytest.fixture(autouse=True)
def _reset():
    breaker("ai").record_success()
    yield
    breaker("ai").record_success()


# ── 의료진별 키 (회신 A-1) ──────────────────────────────────

def test_each_clinician_gets_own_key():
    """버킷 키가 API 키라, 키가 같으면 동시성이 직렬화된다."""
    assert KEYS.key_for("12345") != KEYS.key_for("67890")


def test_unknown_clinician_falls_back_to_shared_key():
    assert KEYS.key_for("00000") == "sk-shared"


@pytest.mark.asyncio
async def test_request_carries_the_clinician_key_and_no_store():
    seen = {}

    def h(req):
        seen["auth"] = req.headers.get("authorization")
        seen["nostore"] = req.headers.get("x-no-store")
        return httpx.Response(200, json=result())

    await ai(h).search("q", clinician_id="67890", collection="cerno_org", where=None)
    assert seen["auth"] == "Bearer sk-b"
    assert seen["nostore"] == "1"      # PHI 비저장 — 전역 차단과 이중


# ── 접근범위는 서버가 계산한다 ──────────────────────────────

@pytest.mark.asyncio
async def test_private_filter_bound_to_identity():
    seen = []

    def h(req):
        import json as _j
        body = _j.loads(req.content)
        seen.append((body["collection"], body.get("filter")))
        return httpx.Response(200, json=result())

    await search_evidence("q", ident=ME, groups=[], ai=ai(h))
    by_col = dict(seen)
    assert by_col["cerno_priv"] == {"owner": "12345"}
    assert by_col["cerno_org"] is None
    assert "cerno_grp" not in by_col          # 그룹 없으면 아예 조회하지 않는다


# ── RRF 병합 ────────────────────────────────────────────────

def test_rrf_is_rank_based_not_score_based():
    """컬렉션마다 점수 분포가 달라 원점수 정렬은 한쪽이 상위를 독식한다."""
    priv = CollectionScope("cerno_priv", {"owner": "x"}, "private")
    org = CollectionScope("cerno_org", None, "org")
    merged = rrf_merge([
        (priv, [Hit("p1", "t", 0.52, {})]),          # 낮은 원점수, 1순위
        (org, [Hit("o1", "t", 0.91, {}), Hit("o2", "t", 0.90, {})]),
    ])
    ids = [h.doc_id for h, _, _ in merged]
    assert ids[0] in ("p1", "o1")                     # 둘 다 1순위 → 동률
    assert ids.index("o2") == 2                       # 2순위는 뒤로


def test_weights_promote_private_shelf():
    """개인 서고 우선 인용(Phase 4-A2)."""
    priv = CollectionScope("cerno_priv", {"owner": "x"}, "private")
    org = CollectionScope("cerno_org", None, "org")
    pairs = [(priv, [Hit("p1", "t", 0.5, {})]), (org, [Hit("o1", "t", 0.9, {})])]
    weighted = rrf_merge(pairs, weights={"private": 1.5})
    assert weighted[0][0].doc_id == "p1"


# ── 근거 게이트 ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_below_threshold_is_not_grounded():
    """임계 미달이면 생성하지 않는다 — 추측으로 메우지 않는다."""
    def h(req):
        return httpx.Response(200, json=result(("d1", "무관한 내용", 0.40, {"visibility": "org"})))

    r = await search_evidence("q", ident=ME, groups=[], ai=ai(h), confidence_min=0.45)
    assert r.grounded is False
    assert r.evidence == []
    assert "임계" in r.reason


@pytest.mark.asyncio
async def test_above_threshold_is_grounded():
    def h(req):
        return httpx.Response(200, json=result(("d1", "관련 내용", 0.71, {"visibility": "org", "citation": "NEJM 2025"})))

    r = await search_evidence("q", ident=ME, groups=[], ai=ai(h), confidence_min=0.45)
    assert r.grounded is True
    assert r.evidence[0].confidence == 0.71


@pytest.mark.asyncio
async def test_gate_applied_before_context_so_llm_never_sees_noise():
    """게이트를 통과 못 한 근거는 evidence 에 아예 담기지 않는다."""
    def h(req):
        import json as _j
        col = _j.loads(req.content)["collection"]
        if col != "cerno_org":
            return httpx.Response(200, json=result())
        return httpx.Response(200, json=result(
            ("good", "관련", 0.70, {}), ("noise", "무관", 0.30, {})))

    r = await search_evidence("q", ident=ME, groups=[], ai=ai(h), confidence_min=0.45)
    assert [e.doc_id for e in r.evidence] == ["good"]


# ── 중복 제거 (AI 서버 v2.124.17 동일 클래스) ───────────────

def test_chunks_of_same_doc_collapse_to_one():
    """dedup 없으면 한 문서의 청크가 상위 슬롯을 잠식해 다른 근거가 밀려난다."""
    org = CollectionScope("cerno_org", None, "org")
    merged = rrf_merge([(org, [
        Hit("guide__chunk_0", "a", 0.80, {}),
        Hit("guide__chunk_1", "b", 0.78, {}),
        Hit("guide__chunk_2", "c", 0.77, {}),
        Hit("other", "d", 0.70, {}),
    ])])
    ids = [h.doc_id for h, _, _ in merged]
    assert len(ids) == 2, "guide 청크 3개가 1건으로 접혀야 한다"
    assert ids[0] == "guide__chunk_0", "대표는 가장 높은 순위의 청크"
    assert "other" in ids, "다른 문서가 밀려나지 않아야 한다"


def test_score_is_max_not_sum_so_chunk_count_does_not_win():
    """합산하면 청크가 많은 문서가 내용과 무관하게 상위로 올라간다."""
    org = CollectionScope("cerno_org", None, "org")
    merged = rrf_merge([(org, [
        Hit("top", "a", 0.9, {}),
        *[Hit(f"many__chunk_{i}", "b", 0.5, {}) for i in range(10)],
    ])])
    assert merged[0][0].doc_id == "top"


@pytest.mark.asyncio
async def test_same_doc_across_collections_appears_once():
    def h(req):
        return httpx.Response(200, json=result(("dup", "같은 문서", 0.70, {})))

    r = await search_evidence("q", ident=ME, groups=[], ai=ai(h))
    assert len(r.evidence) == 1


@pytest.mark.asyncio
async def test_overfetch_requests_more_than_top_k_to_survive_dedup():
    """dedup 후 미보충하면 슬롯이 줄어 권위 위계가 훼손된다."""
    seen = {}

    def h(req):
        import json as _j
        seen["n"] = _j.loads(req.content)["n_results"]
        return httpx.Response(200, json=result())

    await search_evidence("q", ident=ME, groups=[], ai=ai(h), n_per_collection=5)
    assert seen["n"] == 10          # OVERFETCH=2


# ── 인용 라벨: 개인 자료 내용 비노출 ────────────────────────

@pytest.mark.asyncio
async def test_private_evidence_citation_is_opaque():
    def h(req):
        import json as _j
        col = _j.loads(req.content)["collection"]
        if col == "cerno_priv":
            return httpx.Response(200, json=result(
                ("p1", "내 미발표 관찰 기록", 0.80, {"title": "미발표 연구"})))
        return httpx.Response(200, json=result())

    r = await search_evidence("q", ident=ME, groups=[], ai=ai(h))
    e = r.evidence[0]
    assert e.citation == "개인 자료 (비공개)"
    assert "미발표" not in e.citation


# ── degrade ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_one_collection_failure_degrades_but_keeps_rest():
    def h(req):
        import json as _j
        if _j.loads(req.content)["collection"] == "cerno_priv":
            return httpx.Response(500)
        return httpx.Response(200, json=result(("o1", "관련", 0.70, {})))

    r = await search_evidence("q", ident=ME, groups=[], ai=ai(h))
    assert r.grounded is True
    assert r.degraded is True


@pytest.mark.asyncio
async def test_429_is_raised_not_swallowed():
    """혼잡은 전체에 해당한다 — 빈 근거로 답하면 안 된다."""
    def h(req):
        return httpx.Response(429, json={"retry_after": 0})

    with pytest.raises(AiBusy):
        await search_evidence("q", ident=ME, groups=[], ai=ai(h))


# ── 🔴 모드별 유사도 필드가 다르다 ─────────────────────────

@pytest.mark.asyncio
async def test_hybrid_reads_confidence_not_rrf_score():
    """hybrid 의 score 는 RRF 순위 값(~0.01)이다. 그걸 읽으면 임계 0.45 에 전건이
    걸려 Cerno 가 아무 답도 못 한다."""
    def h(req):
        return httpx.Response(200, json={"results": [
            {"id": "d1", "text": "t", "score": 0.0167, "confidence": 0.6909, "metadata": {}}]})

    hits = await ai(h).search("q", clinician_id="12345", collection="cerno_org",
                              where=None, mode="hybrid")
    assert hits[0].score == 0.6909


@pytest.mark.asyncio
async def test_vector_falls_back_to_score_when_confidence_absent():
    """vector 는 코사인을 score 에 담고 confidence 는 null 이다."""
    def h(req):
        return httpx.Response(200, json={"results": [
            {"id": "d1", "text": "t", "score": 0.6909, "confidence": None, "metadata": {}}]})

    hits = await ai(h).search("q", clinician_id="12345", collection="cerno_org", where=None)
    assert hits[0].score == 0.6909


@pytest.mark.asyncio
async def test_fts_only_hit_without_any_similarity_is_dropped():
    """confidence·score 둘 다 없으면 판정 불가다. RRF 로 대체하면 스케일이 달라
    임계 비교가 무의미해지므로 통과시키지 않고 버린다."""
    def h(req):
        return httpx.Response(200, json={"results": [
            {"id": "keep", "text": "t", "confidence": 0.70, "metadata": {}},
            {"id": "drop", "text": "t", "confidence": None, "score": None, "metadata": {}}]})

    hits = await ai(h).search("q", clinician_id="12345", collection="cerno_org",
                              where=None, mode="hybrid")
    assert [x.doc_id for x in hits] == ["keep"]


@pytest.mark.asyncio
async def test_429_does_not_trip_circuit():
    def h(req):
        return httpx.Response(429, json={"retry_after": 0})

    with pytest.raises(AiBusy):
        await ai(h).search("q", clinician_id="12345", collection="cerno_org", where=None)
    assert breaker("ai").state == "closed", "혼잡은 장애가 아니다"


# ── 🔴 hybrid 금지 (라이브 실측 2026-07-30) ─────────────────

@pytest.mark.asyncio
async def test_hybrid_allowed_after_server_fix():
    """AI 서버 v2.124.65 에서 필터 우회가 봉합돼 금지를 해제했다(재검증 완료)."""
    def h(req):
        return httpx.Response(200, json={"results": [
            {"id": "d1", "text": "t", "score": 0.0167, "confidence": 0.6909, "metadata": {}}]})

    hits = await ai(h).search("q", clinician_id="12345", collection="cerno_org",
                              where=None, mode="hybrid")
    assert len(hits) == 1


@pytest.mark.asyncio
async def test_search_uses_vector_mode_by_default():
    """hybrid 도 안전해졌지만 기본값은 vector 를 유지한다 — 임계 0.45 와 N=10 기준선을
    vector 로 잡았으므로, 기본값을 바꾸면 측정된 시스템이 조용히 달라진다."""
    seen = {}

    def h(req):
        import json as _j
        seen["mode"] = _j.loads(req.content)["mode"]
        return httpx.Response(200, json=result())

    await search_evidence("q", ident=ME, groups=[], ai=ai(h))
    assert seen["mode"] == "vector"
