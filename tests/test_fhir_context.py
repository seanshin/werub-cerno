"""FHIR 환자 맥락 계약 테스트 (Phase 3 후속 — 안전 루프 닫기).

핵심: **최소 수집** — 식별자를 꺼내지 않는다. 그리고 **투약 조회 실패를 '금기 없음'으로
읽지 않는다** — HIS 가드 교체 전에는 이 상태가 상시 발생한다.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.fhir.context import build
from cerno.safety.dur import DurResult
from cerno.safety.guard import finalize
from cerno.ask.search import Evidence, SearchResult


def cc(text=None, display=None, code=None):
    n = {}
    if text:
        n["text"] = text
    if display or code:
        n["coding"] = [{k: v for k, v in (("display", display), ("code", code)) if v}]
    return n


def grounded():
    return SearchResult(evidence=[Evidence("d1", "본문", "org", 0.71, "지침", 1)],
                        grounded=True)


# ── 최소 수집 — 식별자를 꺼내지 않는다 ─────────────────────

def test_patient_demographics_are_not_extracted():
    """마스킹은 빠뜨릴 수 있지만 꺼내지 않은 값은 샐 수 없다."""
    ctx = build({"Patient": [{"name": [{"family": "김", "given": ["환자"]}],
                              "identifier": [{"value": "MRN-99999"}],
                              "telecom": [{"value": "010-1234-5678"}]}],
                 "Condition": [{"code": cc("심부전")}]})
    text = ctx.as_text()
    assert "김" not in text and "MRN-99999" not in text and "010" not in text
    assert "심부전" in text


# ── 임상 항목 추출 ──────────────────────────────────────────

def test_conditions_and_labs_summarised():
    ctx = build({
        "Condition": [{"code": cc("만성 심부전")}, {"code": cc(display="제2형 당뇨")}],
        "Observation": [{"code": cc("eGFR"), "valueQuantity": {"value": 42, "unit": "mL/min"}}],
    })
    t = ctx.as_text()
    assert "만성 심부전" in t and "제2형 당뇨" in t
    assert "eGFR 42mL/min" in t


def test_medications_extracted_for_dur():
    """안전 루프의 입력 — HIS MedicationRequest → DUR 점검."""
    ctx = build({"MedicationRequest": [
        {"medicationCodeableConcept": cc("와파린")},
        {"medicationReference": {"display": "아스피린"}},
        {"medicationCodeableConcept": cc("와파린")},      # 중복
    ]})
    assert ctx.drugs == ["와파린", "아스피린"]           # 순서 보존·중복 제거
    assert ctx.medications_available is True


def test_limits_prevent_context_from_crowding_out_evidence():
    """컨텍스트가 넘치면 근거가 밀려난다(슬롯 8192 공유)."""
    ctx = build({"Condition": [{"code": cc(f"상병{i}")} for i in range(50)]})
    assert ctx.as_text().count(",") < 20


# ── 🔴 투약 조회 실패를 '금기 없음' 으로 읽지 않는다 ────────

def test_missing_medication_resource_is_flagged_not_assumed_safe():
    """HIS 가드 교체 전에는 MedicationRequest 가 401/403 이라 키 자체가 없다."""
    ctx = build({"Condition": [{"code": cc("심부전")}]})
    assert ctx.medications_available is False
    assert ctx.drugs == []


def test_dur_says_incomplete_when_meds_unavailable():
    d = DurResult().with_meds_unavailable()
    assert d.needs_review is True
    assert "불완전" in d.summary()
    assert "확인된 DUR 금기·주의가 없습니다" not in d.summary()


def test_clean_dur_with_unavailable_meds_still_warns():
    """경고가 0건이어도 점검 대상이 불완전했으면 '금기 없음' 이 아니다."""
    d = DurResult(warnings={"contraindications": []}, available=True).with_meds_unavailable()
    a = finalize("답변", query="이 약 먹어도 되나요", search=grounded(), dur=d)
    assert a.needs_human_review is True
    assert "불완전" in a.answer
    assert any("투약 목록" in r for r in a.review_reasons)


def test_real_warnings_survive_the_unavailable_flag():
    d = DurResult(warnings={"pregnancy_warnings": [{"x": 1}]},
                  available=True).with_meds_unavailable()
    assert d.total == 1
    assert "임부금기 1건" in d.summary()
    assert "불완전" in d.summary()


# ── 🔴 요청 환자 ≠ launch 환자 (HIS 2026-07-30 가드 변경 대응) ──

def test_patient_mismatch_must_be_rejected_not_silently_substituted():
    """HIS 가 검색 시 subject 를 **토큰 환자로 강제 주입**하도록 바뀌었다.
    다른 환자 id 를 보내도 400 이 아니라 토큰 환자 데이터가 조용히 돌아온다.
    Cerno 가 이를 거르지 않으면 환자 B 질문에 환자 A 맥락을 붙여 답하게 된다 —
    화면에는 B, 근거는 A. 가장 위험한 혼동이다."""
    import hashlib, hmac, os, time
    import httpx
    from fastapi.testclient import TestClient

    os.environ.setdefault("CERNO_API_KEY", "test-api-key")
    os.environ.setdefault("CERNO_AI_BASE", "https://ai.example")
    os.environ.setdefault("CERNO_FHIR_BASE", "https://his.example/fhir/R4")
    from cerno.api.main import app
    import cerno.api.ask as ask_api
    from cerno.fhir import FhirClient
    from cerno.fhir.tokens import SmartToken

    key = os.environ["CERNO_API_KEY"]
    exp = int(time.time()) + 60
    sig = hmac.new(key.encode(), f"12345.clinician.{exp}".encode(), hashlib.sha256).hexdigest()
    h = {"X-Cerno-User": "12345", "X-Cerno-Role": "clinician",
         "X-Cerno-Sig": sig, "X-Cerno-Exp": str(exp)}

    # launch 컨텍스트는 환자 P-A
    tok = SmartToken(access_token="t", patient="P-A", iss="https://his.example")
    stub = FhirClient(tok, base_url="https://his.example/fhir/R4",
                      transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})))

    async def _for(_cid, **kw):
        return stub

    orig = FhirClient.for_clinician
    FhirClient.for_clinician = classmethod(lambda cls, cid, **kw: _for(cid, **kw))
    try:
        c = TestClient(app)
        r = c.post("/ask", headers=h, json={
            "query": "이 환자 처방은?",
            "patient_context": {"source": "emr", "patient_id": "P-B"},   # 다른 환자
        })
        assert r.status_code == 409, f"조용히 대체되면 안 된다 (got {r.status_code})"
        assert "진료 컨텍스트" in r.json()["detail"]
    finally:
        FhirClient.for_clinician = orig


def test_demographics_line_from_patient_resource():
    """성별·연령은 첫 줄로 — 이름 등 인적사항은 여전히 버린다(최소 수집 원칙)."""
    from cerno.fhir.context import build
    ctx = build({"Patient": [{"gender": "male", "birthDate": "1977-03-01",
                              "name": [{"text": "강민준"}], "id": "P-1"}]})
    assert ctx.lines[0].startswith("환자: 남성, ")
    assert ctx.lines[0].endswith("세")
    assert "강민준" not in ctx.as_text()
    assert "P-1" not in ctx.as_text()


def test_demographics_absent_is_silent():
    from cerno.fhir.context import build
    assert build({"Patient": [{}]}).lines == []


def test_demographics_field_for_banner():
    """patient_summary 배너용 (17 P1-7) — 인구학 2필드만, 이름·번호 비수집 유지."""
    ctx = build({"Patient": [{"gender": "male", "birthDate": "1977-03-01",
                              "name": [{"family": "김"}]}]})
    assert ctx.demographics.startswith("남성, ")
    assert "김" not in ctx.demographics
    assert build({"Patient": [{}]}).demographics == ""
