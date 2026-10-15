"""워밍 계약 테스트 — 상주 해제 운영(AI 서버 요청 시 탑재·KEEP_ALIVE 5m) 대응.

핵심: `/warm` 도 신원 없이는 안 된다(익명 GPU 적재 유발 차단) · 워밍 실패는 항상
무해하다 · 파이프라인은 워밍을 **기다리지 않는다**(콜드 2.5~3.9초가 응답에 직렬
가산되던 공백의 회귀 방지).
"""
import asyncio
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

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

ME = "12345"


def headers(uid=ME, role="clinician"):
    exp = int(time.time()) + 60
    sig = hmac.new(KEY.encode(), f"{uid}.{role}.{exp}".encode(), hashlib.sha256).hexdigest()
    return {"X-Cerno-User": uid, "X-Cerno-Role": role,
            "X-Cerno-Sig": sig, "X-Cerno-Exp": str(exp)}


@pytest.fixture(autouse=True)
def _reset():
    breaker("ai").record_success()
    yield
    breaker("ai").record_success()


def make_ai(handler) -> AiClient:
    return AiClient(base_url="https://ai.example",
                    keys=KeyResolver(mapping={ME: "sk-a"}),
                    transport=httpx.MockTransport(handler))


# ── POST /warm ──────────────────────────────────────────────

def test_warm_requires_identity(monkeypatch):
    calls = []
    monkeypatch.setattr("cerno.api.ask.get_ai",
                        lambda: make_ai(lambda r: calls.append(r) or httpx.Response(200, json={})))
    r = TestClient(app).post("/warm")
    assert r.status_code == 401
    assert not calls                       # 무인증 요청이 GPU 적재를 유발하면 안 된다


def test_warm_calls_upstream_and_returns_ok(monkeypatch):
    calls = []

    def handler(req):
        calls.append(str(req.url.path))
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("cerno.api.ask.get_ai", lambda: make_ai(handler))
    r = TestClient(app).post("/warm", headers=headers())
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert calls == ["/api/models/warm"]


def test_warm_upstream_failure_is_still_ok(monkeypatch):
    monkeypatch.setattr("cerno.api.ask.get_ai",
                        lambda: make_ai(lambda r: httpx.Response(500, text="boom")))
    r = TestClient(app).post("/warm", headers=headers())
    assert r.status_code == 200            # 워밍은 최적화 — 실패를 사용자에게 전가하지 않는다


# ── 파이프라인은 워밍을 기다리지 않는다 ─────────────────────

def test_pipeline_does_not_block_on_warm(monkeypatch):
    """warm 이 무한히 걸려도(콜드 최악 시나리오) 질의는 완주해야 한다."""
    from cerno.ask.pipeline import ask as run_ask

    ai = make_ai(lambda r: httpx.Response(200, json={"count": 0, "results": []}))

    started = asyncio.Event()
    release = asyncio.Event()

    async def hanging_warm(**kw):
        started.set()
        await release.wait()

    monkeypatch.setattr(ai, "warm", hanging_warm)

    async def scenario():
        answer = await asyncio.wait_for(
            run_ask("질문", ident=Identity(ME, "clinician"), groups=[], ai=ai),
            timeout=5,
        )
        assert started.is_set()            # 워밍은 발사됐고
        assert not release.is_set()        # 완료 전에 답이 돌아왔다
        release.set()                      # 태스크 정리 후 종료
        await asyncio.sleep(0)
        return answer

    answer = asyncio.run(scenario())
    assert answer.grounded is False        # 검색 0건 → 생성하지 않음(기존 계약 그대로)
