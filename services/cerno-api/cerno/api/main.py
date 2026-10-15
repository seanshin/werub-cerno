"""cerno-api — FastAPI 앱.

**외부에 직접 노출하지 않는다.** cerno-web 프록시만 호출하며, 프록시가 세션을 검증하고
신원을 서명해 붙인다. api 도 독립 검증한다(불변 규칙 6).
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ..circuit import all_states
from ..clients.ai import load_key_map
from ..eval import collector, faithfulness
from ..obs import embed_probe, key_expiry, metrics
from ..obs.scrub import hash_id
from ..obs.scrub import install as install_scrubbers
from . import ask, library
from .deps import clinician_ids, get_ai

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")

_KEY_CHECK_INTERVAL_S = 6 * 3600
_SNAPSHOT_INTERVAL_S = 24 * 3600


async def _key_expiry_loop() -> None:
    # 기동 시 로그만 남기면 장수 프로세스가 D-7 을 조용히 지나친다 — 주기 재점검
    while True:
        await asyncio.sleep(_KEY_CHECK_INTERVAL_S)
        key_expiry.check_and_log()


def snapshot_path() -> str:
    """ask_log 와 같은 볼륨에 둔다 — 재기동·재배포를 넘어 2주 추이가 남는 곳."""
    p = os.environ.get("CERNO_METRICS_SNAPSHOT")
    if p:
        return p
    db = os.environ.get("CERNO_DB_PATH", "cerno.db")
    return os.path.join(os.path.dirname(db) or ".", "metrics-daily.jsonl")


async def _snapshot_loop() -> None:
    while True:
        await asyncio.sleep(_SNAPSHOT_INTERVAL_S)
        metrics.persist_snapshot(snapshot_path())


_EMBED_PROBE_INTERVAL_S = 3600
"""저빈도 고정 — 임베딩 프로브가 embed 모델을 상주시키면 안 된다(17 P0-5)."""


async def _embed_probe_loop() -> None:
    while True:
        await asyncio.sleep(_EMBED_PROBE_INTERVAL_S)
        await embed_probe.run_once()


_FAITHFULNESS_INTERVAL_S = 24 * 3600


async def _faithfulness_loop() -> None:
    """수집 버퍼를 일 1회 AI 서버 야간 스풀에 제출 (17 기획 P0-3).

    submit() 은 AI 장애를 값으로 돌려주므로 이 루프는 죽지 않는다.
    """
    while True:
        await asyncio.sleep(_FAITHFULNESS_INTERVAL_S)
        samples = collector.drain()
        if not samples:
            continue
        r = await faithfulness.submit(get_ai(), "eval-batch", samples)
        logging.getLogger("cerno.eval.faith").info(
            "일일 충실도 제출: 후보 %d건 → %s", len(samples), r)


_key_map_size = 0
"""기동 검증에서 채움 — /metrics 로 노출해 '키맵 0건' 을 밖에서 감지한다 (17 P0-4)."""


def startup_key_check() -> int:
    """CERNO_AI_KEYS 기동 검증 — 비JSON 이면 여기서 죽는다(기동 거부).

    D-0 사고: 비JSON 키맵이 빈 맵으로 조용히 강하 → 전용 키 분리(게이트 3) 무효.
    등록 의료진 중 전용 키 없는 id 는 해시로 경고한다(원문 id 는 로그 금지).
    """
    key_map = load_key_map()          # RuntimeError → uvicorn 기동 실패
    unmapped = [i for i in clinician_ids() if i not in key_map]
    if unmapped:
        logging.getLogger("cerno.api").warning(
            "전용 키 없는 등록 의료진 %d명: %s — 공유 키로 동작(동시성 버킷 공유)",
            len(unmapped), ",".join(hash_id(i) for i in unmapped))
    return len(key_map)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # uvicorn 로깅 설정 완료 후에 붙여야 access 로거에 확실히 부착된다
    install_scrubbers()
    global _key_map_size
    _key_map_size = startup_key_check()
    # 저장 경로 조기 개통 — 첫 검수/질의 시점이 아니라 기동 시점에 권한·경로 문제를 잡는다
    ask.get_review_log()
    ask.get_ask_log()
    key_expiry.check_and_log()
    tasks = [asyncio.create_task(_key_expiry_loop()),
             asyncio.create_task(_snapshot_loop()),
             asyncio.create_task(_faithfulness_loop()),
             asyncio.create_task(_embed_probe_loop())]
    yield
    for t in tasks:
        t.cancel()
    # 종료 직전 최종 스냅샷 — 재배포로 인메모리 카운터가 리셋되기 전의 마지막 기록
    metrics.persist_snapshot(snapshot_path(), reason="shutdown")


app = FastAPI(title="Cerno API", version="0.1.0", docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.include_router(library.router)
app.include_router(ask.router)


@app.get("/metrics")
def observability():
    """운영 지표. PHI·질의 원문·의료진 원문 id 를 담지 않는다."""
    return {**metrics.snapshot(),
            # status() 는 만료일 미설정이면 None — 언패킹 전에 빈 dict 로 강하
            "ai_key": {**(key_expiry.status() or {}), "mapped_clinicians": _key_map_size},
            "ai_embed": embed_probe.probe.status()}


@app.get("/health")
def health():
    """신원 없이 도달 가능한 유일한 경로. PHI·식별자를 담지 않는다."""
    return {"ok": True, "circuits": all_states()}
