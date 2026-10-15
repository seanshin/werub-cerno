"""운영 지표 — 사양서 §11.

**PHI·질의 원문을 담지 않는다.** 카운터와 지연 분포만 들고, 의료진 구분이 필요하면
해시를 쓴다.

무엇을 세는지가 곧 무엇을 관리하는지다. 넷을 고른 이유:
  grounded_ratio    근거 없이 답하지 않는다는 약속이 지켜지는지
  review_ratio      고위험 플래그가 과탐/미탐으로 기우는지
  busy_total        `max_global=3` 천장에 얼마나 부딪히는지 = **GPU 증설 판단의 1차 지표**
  degraded_total    지식 저장소·HIS 조회가 조용히 부분 실패하고 있는지
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field

log = logging.getLogger("cerno.obs.metrics")

_MAX_SAMPLES = 512


@dataclass
class _State:
    counters: Counter = field(default_factory=Counter)
    latencies: deque = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES))
    queue_waits: deque = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES))
    suggest_latencies: deque = field(default_factory=lambda: deque(maxlen=_MAX_SAMPLES))
    lock: threading.Lock = field(default_factory=threading.Lock)


_s = _State()


def record_ask(
    *, grounded: bool, needs_review: bool, degraded: bool,
    latency_s: float, refused: bool = False,
) -> None:
    with _s.lock:
        _s.counters["ask_total"] += 1
        _s.counters["ask_grounded"] += int(grounded)
        _s.counters["ask_review"] += int(needs_review)
        _s.counters["ask_degraded"] += int(degraded)
        _s.counters["ask_refused"] += int(refused)
        _s.latencies.append(latency_s)


def record_busy() -> None:
    """`ServingBusy`(429). 이 값이 오르면 동시 진료실 수요가 천장을 넘고 있다."""
    with _s.lock:
        _s.counters["serving_busy"] += 1


def record_queue(waited_ms: float, position: int | None, active: int | None) -> None:
    """AI 서버 공정 큐 대기(`X-Queue-Waited-Ms`, v2.124.66).

    ★ 이것이 `serving_busy` 보다 민감한 신호다. 단일 키 직렬화는 **429 를 내지 않고
    조용히 느려지므로**(06_동시성_실측.md) 카운터로는 안 잡힌다. 대기 시간을 p95 와
    함께 봐야 "Cerno 가 느린 것" 과 "큐에서 기다린 것" 을 분리할 수 있다.

    알람은 `queue_wait_ratio`(대기 발생 비율)나 대기 p95 로 잡는다.
    """
    with _s.lock:
        _s.counters["queue_calls"] += 1
        if waited_ms > 0:
            _s.counters["queue_waited"] += 1
        _s.queue_waits.append(float(waited_ms))
        if position:
            _s.counters["queue_position_max"] = max(
                _s.counters.get("queue_position_max", 0), int(position))
        if active:
            _s.counters["queue_active_max"] = max(
                _s.counters.get("queue_active_max", 0), int(active))


def record_review(*, usefulness: str, citation_ok: bool | None, missing: bool) -> None:
    """의료진 검수(13_파일럿_운영계획.md §4). '오도율'이 곧 중단 판단의 입력이다."""
    with _s.lock:
        _s.counters["review_total"] += 1
        _s.counters["review_misleading"] += int(usefulness == "misleading")
        _s.counters["review_citation_bad"] += int(citation_ok is False)
        _s.counters["review_missing"] += int(missing)


def record_ingest(*, ok: bool, chunks: int) -> None:
    with _s.lock:
        _s.counters["ingest_total"] += 1
        _s.counters["ingest_failed"] += int(not ok)
        _s.counters["ingest_chunks"] += chunks


def record_suggest(*, questions: int, latency_s: float | None = None) -> None:
    """추천 질문 계측 (17 기획 P0-6) — 5초+ 걸리는 기능인데 사용률·실패율이 어느
    지표에도 없었다. 빈 목록 폴백(설계상 무해)이 얼마나 잦은지가 회고 입력이다."""
    with _s.lock:
        _s.counters["suggest_total"] += 1
        if questions == 0:
            _s.counters["suggest_empty"] += 1
        if latency_s is not None:
            _s.suggest_latencies.append(float(latency_s))


def _pct(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    return round(sorted_vals[min(len(sorted_vals) - 1, int(len(sorted_vals) * p))], 1)


def snapshot() -> dict:
    with _s.lock:
        c = dict(_s.counters)
        lat = sorted(_s.latencies)
        qw = sorted(_s.queue_waits)
        sg = sorted(_s.suggest_latencies)
    total = c.get("ask_total", 0)

    def ratio(k: str) -> float | None:
        return round(c.get(k, 0) / total, 4) if total else None

    def pct(p: float) -> float | None:
        if not lat:
            return None
        return round(lat[min(len(lat) - 1, int(len(lat) * p))], 3)

    rt = c.get("review_total", 0)
    return {
        "counters": c,
        "ratios": {
            "grounded": ratio("ask_grounded"),
            "needs_review": ratio("ask_review"),
            "degraded": ratio("ask_degraded"),
            "model_refused": ratio("ask_refused"),
        },
        # 검수 파생 비율 (17 기획 P0-2) — 오도율은 파일럿 중단 판단 입력(13번 §6),
        # 제출율은 "질의마다 검수" 약속(사용안내 §4)의 준수 척도
        "review": {
            "misleading_ratio": (round(c.get("review_misleading", 0) / rt, 4)
                                 if rt else None),
            "submit_ratio": round(rt / total, 4) if total else None,
        },
        "suggest": {
            "p50": _pct(sg, 0.50), "p95": _pct(sg, 0.95), "samples": len(sg),
            "empty_ratio": (round(c.get("suggest_empty", 0) / c["suggest_total"], 4)
                            if c.get("suggest_total") else None),
        },
        "latency_s": {"p50": pct(0.50), "p95": pct(0.95), "samples": len(lat)},
        # 큐 대기 — serving_busy 보다 민감하다(직렬화는 429 없이 느려지기만 한다)
        "queue_wait_ms": {
            "p50": _pct(qw, 0.50), "p95": _pct(qw, 0.95),
            "max": round(qw[-1], 1) if qw else None,
            "waited_ratio": (round(c.get("queue_waited", 0) / c["queue_calls"], 4)
                             if c.get("queue_calls") else None),
            "samples": len(qw),
        },
    }


def persist_snapshot(path: str, *, reason: str = "daily") -> None:
    """스냅샷 1줄 JSONL append — 인메모리 유실 대비 (17 기획 P0-2).

    회고는 이 파일로 2주 추이를 복원한다. 실패는 로그만 — 관측이 서비스를 막지 않는다.
    """
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reason": reason, **snapshot()}
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        log.exception("지표 스냅샷 기록 실패: %s", path)


def reset() -> None:
    """테스트 전용."""
    with _s.lock:
        _s.counters.clear()
        _s.latencies.clear()
        _s.queue_waits.clear()
        _s.suggest_latencies.clear()
