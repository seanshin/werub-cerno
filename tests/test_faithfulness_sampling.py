"""충실도 층화 샘플링 계약 테스트 (Phase 3-4).

핵심: PHI 붙은 답변은 제외(부분 근거가 채점을 오염) · 층화(무작위면 볼 것이 안 잡힘) ·
결정적(무작위성 없음).
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.circuit import breaker
from cerno.clients.ai import AiClient, KeyResolver
from cerno.eval.faithfulness import STRATA, Sample, stratified, submit

ME = "12345"


def s(cid, *, grounded=True, review=False, refused=False, phi=False):
    return Sample(correlation_id=cid, question="q", answer="a", evidence=["e"],
                  prompt_version="v1", grounded=grounded, needs_review=review,
                  refused=refused, had_patient_context=phi)


def ai(handler):
    return AiClient(base_url="https://ai.example",
                    keys=KeyResolver(mapping={ME: "sk-a"}),
                    transport=httpx.MockTransport(handler))


@pytest.fixture(autouse=True)
def _reset():
    breaker("ai").record_success()
    yield
    breaker("ai").record_success()


# ── 🔴 PHI 제외 ─────────────────────────────────────────────

def test_patient_context_answers_are_excluded():
    """맥락을 빼고 지식 근거만 보내면 채점기가 정확한 답변을 contradicted 로 떨어뜨린다
    (AI 서버 07-27 위양성)."""
    picked = stratified([s("a"), s("phi", phi=True), s("b")])
    ids = [p["id"] for p in picked]
    assert "phi" not in ids
    assert "a" in ids and "b" in ids


def test_phi_sample_has_no_stratum():
    assert s("x", phi=True).stratum() is None


# ── 층화 ────────────────────────────────────────────────────

def test_stratum_classification():
    assert s("x").stratum() == "grounded"
    assert s("x", review=True).stratum() == "review"
    assert s("x", refused=True, grounded=False).stratum() == "refused"


def test_strata_caps_are_respected():
    """무작위면 대부분 grounded 라 review·refused 가 안 잡힌다."""
    samples = ([s(f"g{i}") for i in range(50)]
               + [s(f"r{i}", review=True) for i in range(10)]
               + [s(f"f{i}", refused=True, grounded=False) for i in range(10)])
    picked = stratified(samples)
    from collections import Counter
    strata = Counter()
    for p in picked:
        strata["review" if p["meta"]["needs_human_review"]
               else "grounded" if p["meta"]["grounded"] else "refused"] += 1
    assert strata["grounded"] == STRATA["grounded"]     # 20 (50 있어도 상한)
    assert strata["review"] == STRATA["review"]         # 7
    # refused 는 grounded=False 라 위 분기에서 else 로 감
    assert len(picked) == sum(STRATA.values())


def test_deterministic_no_randomness():
    """스크립트가 결정적이어야 재현·디버깅이 된다."""
    samples = [s(f"g{i}") for i in range(30)]
    assert stratified(samples) == stratified(samples)


def test_payload_shape_matches_ai_server_contract():
    p = stratified([s("c1")])[0]
    assert set(p) == {"id", "question", "answer", "evidence", "meta"}
    assert set(p["meta"]) == {"prompt_version", "grounded", "needs_human_review"}


# ── 제출 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_submit_sends_source_cerno():
    seen = {}

    def h(req):
        import json
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "accepted": 1, "rejected": 0})

    r = await submit(ai(h), ME, [s("c1")])
    assert seen["source"] == "cerno"
    assert r["submitted"] == 1


@pytest.mark.asyncio
async def test_submit_skips_when_only_phi_samples():
    called = {"n": 0}

    def h(req):
        called["n"] += 1
        return httpx.Response(200, json={"ok": True})

    r = await submit(ai(h), ME, [s("x", phi=True)])
    assert r["submitted"] == 0
    assert called["n"] == 0, "빈 표본인데 요청이 나갔다"


@pytest.mark.asyncio
async def test_submit_failure_is_swallowed_for_retry():
    """야간 배치라 사용자 대기가 없다 — 실패해도 다음 회차 재시도."""
    def h(req):
        return httpx.Response(500)

    r = await submit(ai(h), ME, [s("c1")])
    assert r["submitted"] == 0
    assert "error" in r
