"""환자 맞춤 추천 질문(POST /suggest) 계약 테스트.

핵심: 최대 5개 상한 · launch 환자 불일치 409(/ask 와 동일 불변) · 모든 실패는
빈 목록으로 수렴(추천은 최적화) · 관대한 JSON 파싱.
"""
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

KEY = "test-api-key"
os.environ.setdefault("CERNO_API_KEY", KEY)
os.environ.setdefault("CERNO_AI_BASE", "https://ai.example")
os.environ.setdefault("CERNO_AI_KEY", "sk-test")
os.environ.setdefault("CERNO_FHIR_BASE", "https://his.example/fhir/R4")

from fastapi.testclient import TestClient          # noqa: E402

import cerno.api.ask as ask_api                     # noqa: E402
from cerno.api.main import app                      # noqa: E402
from cerno.api.ask import _parse_suggestions        # noqa: E402
from cerno.circuit import breaker                   # noqa: E402
from cerno.clients.ai import AiClient, KeyResolver  # noqa: E402
from cerno.fhir import FhirClient                   # noqa: E402
from cerno.fhir.tokens import SmartToken            # noqa: E402

ME = "12345"

MEDS_BUNDLE = {
    "resourceType": "Bundle",
    "entry": [{"resource": {
        "resourceType": "MedicationRequest", "status": "active",
        "medicationCodeableConcept": {"text": "로사르탄정 50mg"}}}],
}


def headers(uid: str = ME) -> dict:
    exp = int(time.time()) + 30
    sig = hmac.new(KEY.encode(), f"{uid}.clinician.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return {"X-Cerno-User": uid, "X-Cerno-Role": "clinician",
            "X-Cerno-Exp": str(exp), "X-Cerno-Sig": sig}


def _fhir_stub(patient: str = "P-A"):
    tok = SmartToken(access_token="t", patient=patient, iss="https://his.example")
    return FhirClient(tok, base_url="https://his.example/fhir/R4",
                      transport=httpx.MockTransport(
                          lambda r: httpx.Response(200, json=MEDS_BUNDLE)))


def _ai(chat_json):
    breaker("ai").record_success()
    def h(req: httpx.Request):
        return httpx.Response(200, json={"message": {"content": chat_json}})
    return AiClient(keys=KeyResolver(mapping={}, fallback="sk-test"),
                    transport=httpx.MockTransport(h))


def _with_stubs(monkeypatch, *, ai, patient="P-A"):
    stub = _fhir_stub(patient)
    async def _for(cls, cid, **kw):
        return stub
    monkeypatch.setattr(FhirClient, "for_clinician", classmethod(_for))
    monkeypatch.setattr(ask_api, "get_ai", lambda: ai)


def test_suggest_caps_at_five_and_normalizes(monkeypatch):
    items = ",".join(
        f'{{"q":"q{i}","category":"금기","risk":"high","basis":"Cr 1.2"}}'
        for i in range(7))
    _with_stubs(monkeypatch, ai=_ai(
        f'{{"questions":[{items}],"topics":["신기능","혈당 조절"]}}'))
    r = TestClient(app).post("/suggest", headers=headers(), json={
        "patient_context": {"patient_id": "P-A"}})
    assert r.status_code == 200
    d = r.json()
    assert len(d["questions"]) == 5
    assert d["questions"][0] == {
        "question": "q0", "category": "금기", "risk": "high", "basis": "Cr 1.2"}
    assert d["topics"] == ["신기능", "혈당 조절"]


def test_suggest_focus_reaches_prompt(monkeypatch):
    bodies: list = []
    breaker("ai").record_success()
    def h(req: httpx.Request):
        import json as _j
        bodies.append(_j.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "{}"}})
    ai = AiClient(keys=KeyResolver(mapping={}, fallback="sk-test"),
                  transport=httpx.MockTransport(h))
    _with_stubs(monkeypatch, ai=ai)
    monkeypatch.setattr(ask_api, "get_ai", lambda: ai)
    r = TestClient(app).post("/suggest", headers=headers(), json={
        "patient_context": {"patient_id": "P-A"}, "focus": "신기능 관련 투약"})
    assert r.status_code == 200
    assert "신기능 관련 투약" in bodies[-1]["messages"][-1]["content"]


def test_suggest_exclude_reaches_prompt(monkeypatch):
    bodies: list = []
    breaker("ai").record_success()
    def h(req: httpx.Request):
        import json as _j
        bodies.append(_j.loads(req.content))
        return httpx.Response(200, json={"message": {"content": "[]"}})
    ai = AiClient(keys=KeyResolver(mapping={}, fallback="sk-test"),
                  transport=httpx.MockTransport(h))
    _with_stubs(monkeypatch, ai=ai)
    monkeypatch.setattr(ask_api, "get_ai", lambda: ai)
    r = TestClient(app).post("/suggest", headers=headers(), json={
        "patient_context": {"patient_id": "P-A"},
        "exclude": ["병용 금기는?"]})
    assert r.status_code == 200
    prompt = bodies[-1]["messages"][-1]["content"]
    assert "병용 금기는?" in prompt and "중복되지 않는" in prompt


def test_suggest_rejects_patient_mismatch(monkeypatch):
    _with_stubs(monkeypatch, ai=_ai("[]"), patient="P-A")
    r = TestClient(app).post("/suggest", headers=headers(), json={
        "patient_context": {"patient_id": "P-B"}})
    assert r.status_code == 409


def test_suggest_model_garbage_falls_back_to_empty(monkeypatch):
    _with_stubs(monkeypatch, ai=_ai("죄송하지만 목록을 만들 수 없습니다."))
    r = TestClient(app).post("/suggest", headers=headers(), json={
        "patient_context": {"patient_id": "P-A"}})
    assert r.status_code == 200
    assert r.json()["questions"] == []


def test_suggest_requires_identity():
    r = TestClient(app).post("/suggest", json={
        "patient_context": {"patient_id": "P-A"}})
    assert r.status_code == 401


def test_parse_is_lenient_with_fenced_json_and_legacy_strings():
    raw = '설명입니다.\n```json\n["병용 금기는?", "  ", 3, {"q":"목표 혈압은?","category":"이상한값","risk":"급함"}]\n```'
    qs, topics = _parse_suggestions(raw)
    assert qs == [
        {"question": "병용 금기는?", "category": "기타", "risk": "medium", "basis": ""},
        {"question": "목표 혈압은?", "category": "기타", "risk": "medium", "basis": ""},
    ]
    assert topics == []


def test_instruction_guards_demographics():
    """성별·나이 불일치 주제(남성에게 임신 등) 배제 지시가 프롬프트에 있다."""
    from cerno.api.ask import _SUGGEST_INSTRUCTION
    assert "성별" in _SUGGEST_INSTRUCTION and "임신" in _SUGGEST_INSTRUCTION


def test_suggest_carries_patient_summary_field(monkeypatch):
    """/suggest 응답에 patient_summary (17 P1-7) — 추가 필드라 하위호환."""
    _with_stubs(monkeypatch, ai=_ai('{"questions":[],"topics":[]}'))
    r = TestClient(app).post("/suggest", json={
        "patient_context": {"patient_id": "P-A"}}, headers=headers())
    assert r.status_code == 200
    assert "patient_summary" in r.json()
