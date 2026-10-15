"""경량 서킷브레이커 — 외부 의존(HIS·AI 서버) 장애 시 빠른 차단.

이식 원본: twin/twin/circuit.py

연속 실패 N회 → open(차단) T초 → 그 동안 호출하지 않고 즉시 실패한다(타임아웃 대기 회피).
프로세스 단위 상태.

⚠️ 429(rate limit)에는 트립하지 않는다 — 서버 장애가 아니라 과부하 신호이므로
백오프로 흡수해야지 회로를 열면 정상 트래픽까지 죽인다.
"""
from __future__ import annotations

import time


class Breaker:
    def __init__(self, name: str, threshold: int = 5, reset: float = 30.0) -> None:
        self.name = name
        self.threshold = threshold
        self.reset = reset
        self.failures = 0
        self.open_until = 0.0

    def allow(self) -> bool:
        return not (self.open_until and time.monotonic() < self.open_until)

    def record_success(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.open_until = time.monotonic() + self.reset

    @property
    def state(self) -> str:
        if self.open_until and time.monotonic() < self.open_until:
            return "open"
        return "closed" if self.failures == 0 else "half"


_breakers: dict[str, Breaker] = {}


def breaker(name: str) -> Breaker:
    return _breakers.setdefault(name, Breaker(name))


def all_states() -> dict[str, str]:
    return {n: b.state for n, b in _breakers.items()}


# 실제 배선된 의존을 사전 등록 — 첫 호출 전에도 관측에 노출되어야 알람이 베이스라인을 잡는다.
KNOWN_CIRCUITS = ("his", "ai")
for _n in KNOWN_CIRCUITS:
    breaker(_n)


class CircuitOpen(RuntimeError):
    pass
