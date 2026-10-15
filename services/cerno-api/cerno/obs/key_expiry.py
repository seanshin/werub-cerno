"""AI 서버 API 키 만료 경보 — 리스크 #7 의 Cerno 측 심층방어.

AI 서버가 발급 시 D-7 경보를 등록해 주지만(11_운영자_요청.md), 그 경보에만 의존하면
저쪽 설정 누락이 곧 우리 장애다 — appServer 가 2026-07-22 만료 무경보로 3일 단절된
클래스. 만료일을 우리 관측에도 실어 이중으로 잡는다.

`CERNO_AI_KEY_EXPIRES`(ISO 날짜, 예: 2027-07-30)에 **사용 중인 키들 중 가장 이른
만료일**을 넣는다. 키가 여럿이어도(파일럿 키 2개+@) 행동은 "가장 급한 것 기준 갱신"
하나뿐이므로 날짜 하나로 충분하다 — 키별 만료를 나열하면 무인증 /metrics 에 키 구성이
드러나는 부작용만 있다.

경보이지 게이트가 아니다 — 어떤 경우에도 요청을 막지 않는다.
"""
from __future__ import annotations

import logging
import os
from datetime import date

log = logging.getLogger("cerno.obs.key_expiry")

WARN_DAYS = 7


def status(*, today: date | None = None) -> dict | None:
    """만료 상태. env 미설정이면 None(관측 항목 자체가 빠져 '미설정'이 보이도록 하지
    않는다 — /metrics 소비자가 null 을 보고 설정 누락을 알아챈다)."""
    raw = (os.environ.get("CERNO_AI_KEY_EXPIRES") or "").strip()
    if not raw:
        return None
    try:
        expires = date.fromisoformat(raw)
    except ValueError:
        log.error("CERNO_AI_KEY_EXPIRES 파싱 실패: %r — 경보 비활성", raw)
        return None

    days_left = (expires - (today or date.today())).days
    return {
        "expires": expires.isoformat(),
        "days_left": days_left,
        "warning": days_left <= WARN_DAYS,
    }


def check_and_log(*, today: date | None = None) -> dict | None:
    """상태를 계산하고 임계에 걸리면 로그로도 경보한다(기동 시·주기 점검용)."""
    st = status(today=today)
    if st is None:
        log.info("CERNO_AI_KEY_EXPIRES 미설정 — 키 만료 경보 비활성 (설정 권장)")
        return None
    if st["days_left"] < 0:
        log.error("🔴 AI 키가 만료됐다 (%s, %d일 경과) — 즉시 재발급 필요",
                  st["expires"], -st["days_left"])
    elif st["warning"]:
        log.warning("⚠️ AI 키 만료 임박 D-%d (%s) — 운영자에게 재발급 요청할 것",
                    st["days_left"], st["expires"])
    return st
