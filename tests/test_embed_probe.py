"""임베딩 능동 프로브 (17 기획 P0-5) — 전이·429 보류·지표 비오염 계약."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

import httpx  # noqa: E402
import pytest  # noqa: E402

from cerno.obs import metrics  # noqa: E402
from cerno.obs.embed_probe import EmbedProbe, run_once  # noqa: E402


def test_degrades_after_two_consecutive_failures():
    p = EmbedProbe()
    p.record("fail")
    assert p.status()["degraded"] is False       # 1회는 일시 오류일 수 있다
    p.record("fail")
    assert p.status()["degraded"] is True


def test_ok_recovers_and_resets():
    p = EmbedProbe()
    p.record("fail"); p.record("fail")
    p.record("ok")
    s = p.status()
    assert s["degraded"] is False and s["consecutive_failures"] == 0
    assert s["last_ok"] is not None


def test_busy_is_inconclusive():
    p = EmbedProbe()
    p.record("fail")
    p.record("busy")                             # 혼잡은 실패도 회복도 아니다
    assert p.status()["consecutive_failures"] == 1
    p.record("fail")
    assert p.status()["degraded"] is True        # busy 가 카운트를 끊지 않는다


@pytest.mark.parametrize("status,expected", [(200, "ok"), (429, "busy"), (503, "fail")])
def test_run_once_classifies_status(monkeypatch, status, expected):
    monkeypatch.setenv("CERNO_AI_BASE", "https://ai.example")

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            return httpx.Response(status_code=status)

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    metrics.reset()
    p = EmbedProbe()
    assert asyncio.run(run_once(p)) == expected
    # 프로브는 AiClient 를 안 탄다 — queue_wait·latency 표본 비오염
    snap = metrics.snapshot()
    assert snap["queue_wait_ms"]["samples"] == 0 and snap["latency_s"]["samples"] == 0


def test_run_once_network_error_is_fail(monkeypatch):
    monkeypatch.setenv("CERNO_AI_BASE", "https://ai.example")

    class FakeClient:
        def __init__(self, **kw): ...
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **kw):
            raise httpx.ReadTimeout("timeout")

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    assert asyncio.run(run_once(EmbedProbe())) == "fail"
