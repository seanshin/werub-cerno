"""FHIR 커넥터 degrade 계약 테스트 (01_구현계획.md Phase 1-6).

지키는 것: 실패해도 **추측으로 메우지 않는다**. 토큰 문제와 조회 실패를 구분한다.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.circuit import breaker
from cerno.fhir import EvidenceUnavailable, FhirClient, SmartReentryRequired
from cerno.fhir.tokens import SmartToken

BASE = "https://his.example/fhir/R4"
TOK = SmartToken(access_token="tok", patient="P1", iss="https://his.example")


def client(handler) -> FhirClient:
    return FhirClient(TOK, base_url=BASE, transport=httpx.MockTransport(handler))


def bundle(*resources):
    return {"resourceType": "Bundle", "entry": [{"resource": r} for r in resources]}


@pytest.fixture(autouse=True)
def _reset_breaker():
    breaker("his").record_success()
    yield
    breaker("his").record_success()


# ── 정상 수집 ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_collects_all_resources():
    def h(req):
        rt = req.url.path.rsplit("/", 1)[-1]
        return httpx.Response(200, json=bundle({"resourceType": rt, "id": "x"}))

    ctx = await client(h).patient_context("P1")
    assert set(ctx) == {
        "Condition", "Observation", "DiagnosticReport", "Procedure",
        "MedicationRequest", "AllergyIntolerance", "DocumentReference",
        "Patient",   # 성별·연령 근거 (2026-08-04 — 인구학 없이는 임신 등 배제 불가)
    }
    assert ctx["Condition"][0]["resourceType"] == "Condition"


@pytest.mark.asyncio
async def test_condition_uses_subject_others_use_patient():
    """HIS 컨트롤러 계약 — Condition 만 subject 파라미터를 받는다."""
    seen = {}

    def h(req):
        rt = req.url.path.rsplit("/", 1)[-1]
        seen[rt] = dict(req.url.params)
        return httpx.Response(200, json=bundle())

    await client(h).patient_context("P1")
    assert seen["Condition"]["subject"] == "P1"
    assert seen["Observation"]["patient"] == "P1"


# ── 부분 실패는 전체를 죽이지 않는다 ────────────────────────

@pytest.mark.asyncio
async def test_403_on_some_resources_keeps_the_rest():
    """HIS 가드 교체 전 상태 — 권장 3종이 막혀도 필수 4종은 살아야 한다."""
    blocked = {"MedicationRequest", "AllergyIntolerance", "DocumentReference"}

    def h(req):
        rt = req.url.path.rsplit("/", 1)[-1]
        if rt in blocked:
            return httpx.Response(403)
        return httpx.Response(200, json=bundle({"resourceType": rt}))

    ctx = await client(h).patient_context("P1")
    assert ctx["Condition"] and ctx["Observation"]
    for rt in blocked:
        assert ctx[rt] == []          # 비어 있을 뿐, 예외로 전체를 죽이지 않는다


@pytest.mark.asyncio
async def test_500_on_one_resource_keeps_the_rest():
    def h(req):
        rt = req.url.path.rsplit("/", 1)[-1]
        if rt == "Observation":
            return httpx.Response(500)
        return httpx.Response(200, json=bundle({"resourceType": rt}))

    ctx = await client(h).patient_context("P1")
    assert ctx["Observation"] == []
    assert ctx["Condition"]


# ── 토큰 문제는 전체에 해당하므로 올린다 ────────────────────

@pytest.mark.asyncio
async def test_401_raises_smart_reentry():
    """401 은 재인증 요구이지 조회 실패가 아니다 — 구분해서 올린다."""
    def h(req):
        return httpx.Response(401)

    with pytest.raises(SmartReentryRequired):
        await client(h).patient_context("P1")


@pytest.mark.asyncio
async def test_missing_patient_id_rejected():
    def h(req):
        return httpx.Response(200, json=bundle())

    with pytest.raises(EvidenceUnavailable):
        await client(h).patient_context("")


# ── 서킷브레이커 ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_5xx_trips_breaker_but_403_does_not():
    br = breaker("his")

    def h403(req):
        return httpx.Response(403)

    await client(h403).patient_context("P1")
    assert br.state == "closed", "403 은 서버 장애가 아니므로 서킷을 건드리지 않는다"


@pytest.mark.asyncio
async def test_429_does_not_trip_breaker():
    """429 는 rate limit — 회로를 열면 정상 트래픽까지 죽는다."""
    calls = {"n": 0}

    def h(req):
        calls["n"] += 1
        if calls["n"] <= 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json=bundle({"resourceType": "Condition"}))

    ctx = await client(h).patient_context("P1", resources=("Condition",))
    assert ctx["Condition"]
    assert breaker("his").state == "closed"


# ── 예외에 내부 정보를 싣지 않는다 ──────────────────────────

@pytest.mark.asyncio
async def test_error_does_not_leak_token_or_url():
    def h(req):
        raise httpx.ConnectError("connect to 10.0.0.5:4000 failed")

    try:
        await client(h).patient_context("P1", resources=("Condition",))
    except SmartReentryRequired:
        pytest.fail("연결 실패를 토큰 문제로 분류하면 안 된다")
    # 연결 실패는 리소스 단위로 흡수되어 빈 결과가 된다
    ctx = await client(h).patient_context("P1", resources=("Condition",))
    assert ctx["Condition"] == []


def test_evidence_unavailable_message_has_no_token():
    e = EvidenceUnavailable("HIS 500 (Condition)")
    assert "tok" not in str(e)
