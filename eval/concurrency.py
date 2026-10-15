"""동시성 실측 (Phase 3-7) — AI 서버 공정 큐 천장을 잰다.

AI 서버 회신 A-1 이 주장한 값을 **검증**한다.
    max_per_user = 1   버킷 = API 키의 sha256
    max_global   = 3
    대기열 20 · 대기 60초 초과 시 429 + retry_after

이 수치가 맞다면 **단일 키로는 의료진이 몇 명이든 동시 1건**이고, 그것이 파일럿에서
가장 먼저 드러날 문제다. 그래서 키를 3개 요청했다 — 이 스크립트가 그 요청의 근거다.

측정 방법: 같은 키로 N개 질의를 동시에 던지고 **완료 시각의 계단 모양**을 본다.
직렬화되면 응답이 1건씩 순차로 떨어지고, 병렬이면 뭉쳐서 떨어진다.

사용:
    python eval/concurrency.py --n 4
    python eval/concurrency.py --n 4 --keys "sk-a,sk-b,sk-c"   # 다중 키 비교
"""
from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "cerno-api"))
os.environ.setdefault("CERNO_CONFIG_DIR", str(ROOT / "config"))

from cerno.auth import Identity                                  # noqa: E402
from cerno.clients.ai import AiBusy, AiClient, KeyResolver       # noqa: E402

QUERY = "심부전 환자에게 어떤 약제를 병용해야 하나요?"


async def _one(ai: AiClient, clinician: str, idx: int, t0: float) -> dict:
    started = time.perf_counter() - t0
    try:
        await ai.chat([{"role": "user", "content": f"{QUERY} (요청 {idx})"}],
                      clinician_id=clinician, num_predict=120)
        err = None
    except AiBusy as e:
        err = f"429 busy (retry_after={e.retry_after})"
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {str(e)[:60]}"
    return {"idx": idx, "clinician": clinician,
            "start": round(started, 3),
            "end": round(time.perf_counter() - t0, 3), "error": err}


async def run(n: int, keys: list[str] | None) -> None:
    if keys:
        # 의료진별 키 — clinician_id 를 키 개수만큼 돌린다
        mapping = {f"doc{i}": k for i, k in enumerate(keys)}
        resolver = KeyResolver(mapping=mapping, fallback="")
        clinicians = [f"doc{i % len(keys)}" for i in range(n)]
        label = f"의료진별 키 {len(keys)}개"
    else:
        resolver = KeyResolver(mapping={}, fallback=os.environ.get("CERNO_AI_KEY", ""))
        clinicians = [f"doc{i}" for i in range(n)]   # 서로 다른 의료진, 같은 키
        label = "단일 키 공유"

    ai = AiClient(keys=resolver)
    print(f"동시 요청 {n}건 · {label}")
    print("워밍업 1회…")
    await ai.chat([{"role": "user", "content": "워밍업"}],
                  clinician_id=clinicians[0], num_predict=8)

    t0 = time.perf_counter()
    results = await asyncio.gather(*(_one(ai, c, i, t0) for i, c in enumerate(clinicians)))
    results.sort(key=lambda r: r["end"])

    print(f"\n{'#':>3} {'clinician':10s} {'완료(s)':>9}  {'소요(s)':>8}  결과")
    prev = 0.0
    for r in results:
        gap = r["end"] - prev
        mark = "  ← 계단" if gap > 0.8 and prev else ""
        print(f"{r['idx']:>3} {r['clinician']:10s} {r['end']:>9.2f}  "
              f"{r['end'] - r['start']:>8.2f}  {r['error'] or 'ok'}{mark}")
        prev = r["end"]

    ends = [r["end"] for r in results]
    durs = [r["end"] - r["start"] for r in results]
    busy = sum(1 for r in results if r["error"] and "429" in r["error"])
    spread = max(ends) - min(ends)

    print("\n" + "-" * 60)
    print(f"  전체 소요      {max(ends):.2f}s")
    print(f"  완료 시각 분산 {spread:.2f}s   (0 에 가까우면 병렬, 크면 직렬)")
    print(f"  개별 지연 중앙 {statistics.median(durs):.2f}s")
    print(f"  429 busy       {busy}건")

    # 판정 — 직렬화 여부는 '완료 분산 / 개별 지연' 비로 본다
    ratio = spread / statistics.median(durs) if durs else 0
    if busy:
        print(f"\n  판정: 대기열 초과 발생 — 천장에 부딪혔다({busy}건 429)")
    elif ratio > 0.6:
        print(f"\n  판정: **직렬화됨** (비 {ratio:.2f}) — 버킷을 공유하고 있다")
    else:
        print(f"\n  판정: 병렬 처리됨 (비 {ratio:.2f})")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=4)
    ap.add_argument("--keys", default="", help="쉼표 구분. 비우면 CERNO_AI_KEY 단일 사용")
    a = ap.parse_args()
    asyncio.run(run(a.n, [k.strip() for k in a.keys.split(",") if k.strip()] or None))
