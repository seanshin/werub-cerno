"""per-user SMART 토큰 조회 — cerno-web 이 콜백에서 저장한 것을 읽는다.

키 규약은 `cerno-web/lib/tokenStore.ts` 와 맞춘다: `cerno:smart:tok:{uid}`.

01_구현계획.md §3.2 불변 규칙 9 · §9 리스크 #9:
저장소 인증을 강제한다. 살아있는 HIS access_token 을 무인증 저장소에 두지 않는다.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import redis.asyncio as aioredis

_AUTHED_URL = re.compile(r"^rediss?://[^@/]*:[^@/]+@")


class SmartReentryRequired(Exception):
    """토큰 부재·만료. 호출측은 근거 조회를 포기하고 SMART 재진입을 유도한다.

    Cerno 세션 TTL(8h)이 SMART 토큰 TTL 보다 길 수 있어 정상적으로 발생하는 상태다.
    오류가 아니라 재인증 요구로 다뤄야 한다.
    """


@dataclass(frozen=True)
class SmartToken:
    access_token: str
    patient: str
    iss: str


def _url() -> str:
    u = os.environ.get("CERNO_REDIS_URL")
    if not u:
        raise RuntimeError("CERNO_REDIS_URL is required")
    if not _AUTHED_URL.match(u):
        raise RuntimeError(
            "CERNO_REDIS_URL must include a password (requirepass). "
            "무인증 저장소에 살아있는 HIS 토큰을 두지 않는다."
        )
    return u


_client: aioredis.Redis | None = None


def _redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(_url(), decode_responses=True)
    return _client


async def get_smart_token(clinician_id: str) -> SmartToken:
    """없거나 만료면 SmartReentryRequired. 절대 빈 토큰으로 진행하지 않는다."""
    raw = await _redis().get(f"cerno:smart:tok:{clinician_id}")
    if not raw:
        raise SmartReentryRequired("SMART 토큰 없음 — 재진입 필요")
    try:
        d = json.loads(raw)
    except ValueError:
        raise SmartReentryRequired("SMART 토큰 손상 — 재진입 필요") from None
    if not d.get("access_token"):
        raise SmartReentryRequired("SMART 토큰 비어 있음 — 재진입 필요")
    return SmartToken(
        access_token=d["access_token"],
        patient=d.get("patient", ""),
        iss=d.get("iss", ""),
    )
