"""이어서 질문(history) 계약 테스트.

핵심: history 는 **생성 문맥으로만** 들어간다 — 검색·근거 게이트는 현 질문만 본다.
서버는 마지막 3개만 쓰고, 저장하지 않는다(PHI 비저장 경계 — 로그에도 남기지 않는다).
"""
import asyncio
import hashlib
import hmac
import json
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

from fastapi.testclient import TestClient          # noqa: E402

from cerno.api.main import app                      # noqa: E402
from cerno.auth import Identity                     # noqa: E402
from cerno.circuit import breaker                   # noqa: E402
from cerno.clients.ai import AiClient, KeyResolver  # noqa: E402
from cerno.ask.pipeline import ask as run_ask       # noqa: E402

ME = "doc-hist"

HIT = {"id": "guide-hf__chunk_0", "score": 0.9, "confidence": 0.9,
       "text": "심부전 4제 병용", "metadata": {
           "citation": "심부전 지침", "visibility": "org", "owner": "sys",
           "source_doc_id": "guide-hf"}}


def make_ai(handler):
    breaker("ai").record_success()      # 이전 테스트가 열어둔 회로 닫기
    return AiClient(keys=KeyResolver(mapping={}, fallback="sk-test"),
                    transport=httpx.MockTransport(handler))


def headers(uid: str = ME) -> dict:
    exp = int(time.time()) + 30
    sig = hmac.new(KEY.encode(), f"{uid}.clinician.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return {"X-Cerno-User": uid, "X-Cerno-Role": "clinician",
            "X-Cerno-Exp": str(exp), "X-Cerno-Sig": sig}


def _capture_chat(chat_bodies: list):
    def h(req: httpx.Request):
        if req.url.path.endswith("/rag/search"):
            return httpx.Response(200, json={"count": 1, "results": [HIT]})
        if req.url.path.endswith("/chat"):
            chat_bodies.append(json.loads(req.content))
            return httpx.Response(200, json={
                "message": {"content": "답변입니다. [1]"}})
        return httpx.Response(200, json={})
    return h


def test_history_becomes_prior_turns_in_generation():
    chats: list = []
    ai = make_ai(_capture_chat(chats))
    answer = asyncio.run(run_ask(
        "그럼 용량은?", ident=Identity(ME, "clinician"), groups=[], ai=ai,
        history=[("심부전 병용 약제는?", "4제 병용입니다."),
                 ("금기는?", "신부전 주의.")]))
    assert answer.grounded is True
    msgs = chats[-1]["messages"]
    roles = [m["role"] for m in msgs]
    # system → (user·assistant)×2 → 최종 user 프롬프트
    assert roles == ["system", "user", "assistant", "user", "assistant", "user"]
    assert msgs[1]["content"] == "심부전 병용 약제는?"
    assert msgs[2]["content"] == "4제 병용입니다."
    assert answer.meta.get("history_turns") == 2
    # 최종 프롬프트(근거 포함)는 마지막 user 턴 하나뿐이다
    assert "심부전 4제 병용" in msgs[-1]["content"]


def test_followup_search_includes_previous_question():
    """이어서 질문은 직전 질문을 검색어에 합친다 — 답변은 합치지 않는다."""
    seen: list = []

    def h(req: httpx.Request):
        if req.url.path.endswith("/rag/search"):
            seen.append(json.loads(req.content)["query"])
            return httpx.Response(200, json={"count": 1, "results": [HIT]})
        return httpx.Response(200, json={"message": {"content": "답. [1]"}})

    ai = make_ai(h)
    asyncio.run(run_ask(
        "그럼 용량은?", ident=Identity(ME, "clinician"), groups=[], ai=ai,
        history=[("심부전 병용 약제는?", "4제 병용입니다.")]))
    assert all("심부전 병용 약제는?" in q and "그럼 용량은?" in q for q in seen)
    assert all("4제 병용입니다" not in q for q in seen)


def test_history_does_not_bypass_evidence_gate():
    """이력이 있어도 검색 0건이면 ungrounded — 게이트는 현 질문만 본다."""
    def h(req: httpx.Request):
        if req.url.path.endswith("/rag/search"):
            return httpx.Response(200, json={"count": 0, "results": []})
        raise AssertionError("근거 없이 생성이 호출됐다")
    ai = make_ai(h)
    answer = asyncio.run(run_ask(
        "그럼 용량은?", ident=Identity(ME, "clinician"), groups=[], ai=ai,
        history=[("심부전 병용 약제는?", "4제 병용입니다.")]))
    assert answer.grounded is False


def test_endpoint_caps_history_to_last_three(monkeypatch):
    chats: list = []
    ai = make_ai(_capture_chat(chats))
    monkeypatch.setattr("cerno.api.ask.get_ai", lambda: ai)
    turns = [{"query": f"질문{i}", "answer": f"답{i}"} for i in range(5)]
    r = TestClient(app).post("/ask", headers=headers(),
                             json={"query": "이어서?", "history": turns})
    assert r.status_code == 200
    msgs = chats[-1]["messages"]
    user_turns = [m["content"] for m in msgs if m["role"] == "user"][:-1]
    assert user_turns == ["질문2", "질문3", "질문4"]      # 마지막 3개만


def test_oversized_history_turn_is_rejected():
    r = TestClient(app).post("/ask", headers=headers(), json={
        "query": "q", "history": [{"query": "q", "answer": "a" * 4001}]})
    assert r.status_code == 422
