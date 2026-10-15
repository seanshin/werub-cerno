"""AI 서버 임베딩 경로 능동 프로브 (17 기획 P0-5).

D-0(2026-08-04) 임베딩 장애는 실사용자 질의가 degraded 로 떨어져서야 발견됐다 —
`/health` 서킷은 수동적이고 주간 골든셋은 주 1회다. 이 프로브가 그 사각을 메운다.

설계 제약:
  - **저빈도(60분).** 검색 프로브는 임베딩을 수반하므로 고빈도로 돌리면 embed 모델을
    사실상 상주시켜 AI 서버의 상주 해제 운영과 충돌한다. AI 서버가 embed 헬스를
    노출하면(16번 §4-3 요청) 그 경로로 교체한다.
  - **지표 비오염.** AiClient 를 타지 않는 독립 호출 — queue_wait·latency 표본에
    프로브가 섞이지 않는다.
  - 429(혼잡)는 실패로 안 친다(서킷 규칙 circuit.py 와 동일 철학) — 판단 보류.
"""
from __future__ import annotations

import logging
import os
import time

import httpx

log = logging.getLogger("cerno.obs.embed_probe")

PROBE_QUERY = "고혈압 목표 혈압"
"""org 서고에 실존하는 주제 — 결과 유무가 아니라 200/503 만 본다."""

_FAIL_THRESHOLD = 2
_TIMEOUT_S = 35.0
"""AI 서버 자체의 임베딩 read timeout(30s) 503 을 기다려 받을 수 있는 값."""


class EmbedProbe:
    def __init__(self, fail_threshold: int = _FAIL_THRESHOLD) -> None:
        self._threshold = fail_threshold
        self._consecutive = 0
        self._degraded = False
        self._last_ok: float | None = None

    def record(self, outcome: str) -> None:
        """outcome: ok | fail | busy. busy 는 판단 보류(카운트 유지)."""
        if outcome == "busy":
            return
        if outcome == "ok":
            if self._degraded:
                log.info("임베딩 경로 회복 (연속 실패 %d회 후)", self._consecutive)
            self._consecutive = 0
            self._degraded = False
            self._last_ok = time.time()
            return
        self._consecutive += 1
        if self._consecutive >= self._threshold and not self._degraded:
            self._degraded = True
            # ERROR 한 줄이 감지 신호다 — 주간 크론 ALERT 패턴과 같은 결로 잡힌다
            log.error("AI 서버 임베딩 경로 장애 의심 — 연속 %d회 실패 (16번 §5 로 확인)",
                      self._consecutive)

    def status(self) -> dict:
        return {"degraded": self._degraded,
                "consecutive_failures": self._consecutive,
                "last_ok": self._last_ok}


probe = EmbedProbe()


async def run_once(p: EmbedProbe | None = None) -> str:
    """1회 프로브 실행 → outcome 을 기록하고 반환. 예외를 밖으로 내지 않는다."""
    p = p or probe
    base = os.environ.get("CERNO_AI_BASE", "").rstrip("/")
    key = os.environ.get("CERNO_AI_KEY", "")
    if not base:
        return "skip"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as c:
            r = await c.post(
                f"{base}/api/rag/search",
                headers={"Authorization": f"Bearer {key}"},
                json={"query": PROBE_QUERY, "collection": "cerno_org",
                      "n_results": 1, "mode": "vector", "rerank": False})
        outcome = "ok" if r.status_code == 200 else (
            "busy" if r.status_code == 429 else "fail")
    except httpx.HTTPError:
        outcome = "fail"
    p.record(outcome)
    return outcome
