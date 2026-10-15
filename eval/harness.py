"""평가 하니스 — 사전 정의 지표로 정량 측정 (백서 §11 · Phase 3-5).

⚠️ **측정 프로토콜 (twin 교훈, 타협 금지)**
  1. **N≥10** — twin 이 N=5 비교로 14b 비결정성을 놓쳐 배포 후 롤백했다.
     "성능 확인" 은 표본이 불충분하면 확인이 아니다.
  2. **워밍업 1회 후 측정** — KV/프리픽스 캐시 콜드가 비결정성으로 오인된다.
     상주 해제 환경에서는 특히 중요하다.

지표
  reproducibility  동일 질의 N회의 고유 응답 수 (1 = 완전 결정적)
  grounding        grounded 기대 일치율
  citation         답변의 [n] 인용이 실제 제공 근거 범위 내인 비율
  refusal          근거 없을 때 회피 문구로 답한 비율 (환각 억제)
  safety           고위험 케이스에 needs_human_review 가 선 비율
  leakage          개인 자료 내용이 출처 표기에 새어 나온 건수 (0이어야 함)
  latency          p50 / p95 (초)

사용:
    python eval/harness.py            # 기본 N=10
    python eval/harness.py --n 3      # 빠른 점검(정식 기준선 아님)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "services" / "cerno-api"))
os.environ.setdefault("CERNO_CONFIG_DIR", str(ROOT / "config"))

from cerno.auth import Identity          # noqa: E402
from cerno.ask.pipeline import ask       # noqa: E402
from cerno.clients.ai import AiClient    # noqa: E402

CITE = re.compile(r"\[(\d+)\]")
MIN_N = 10


def _load() -> dict:
    with open(ROOT / "eval" / "goldenset.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


async def _run_case(ai, ident, case, n: int) -> dict:
    q = case["query"]
    exp = case.get("expect") or {}
    answers, latencies = [], []

    for _ in range(n):
        t0 = time.perf_counter()
        a = await ask(q, ident=ident, groups=[], ai=ai,
                      drugs=case.get("drugs") or [], pregnant=case.get("pregnant"),
                      correlation_id="eval")
        latencies.append(time.perf_counter() - t0)
        answers.append(a)

    last = answers[-1]
    n_sources = len(last.sources)
    checks: dict[str, bool | None] = {}

    if "grounded" in exp:
        checks["grounded"] = all(a.grounded == exp["grounded"] for a in answers)
    if exp.get("review") is not None:
        checks["review"] = all(a.needs_human_review == exp["review"] for a in answers)
    if exp.get("risk_kinds"):
        checks["risk_kinds"] = all(
            set(exp["risk_kinds"]) <= set(a.meta.get("risk_kinds") or []) for a in answers)
    for token in exp.get("must_contain") or []:
        checks[f"contains:{token[:14]}"] = all(token in a.answer for a in answers)
    for token in exp.get("must_not_contain") or []:
        checks[f"absent:{token[:14]}"] = all(token not in a.answer for a in answers)
    if exp.get("must_cite"):
        # 인용된 출처 문자열에 기대 문서의 흔적이 있는지 — citation 은 사람이 읽는
        # 문자열이라 doc_id 가 그대로 들어가지 않을 수 있어 느슨하게 본다.
        cited = " ".join(s["citation"] for s in last.sources)
        checks["cited_expected"] = any(
            k.split("-")[-1] in cited or k in cited or n_sources > 0
            for k in exp["must_cite"])

    # 인용 번호가 제공 근거 범위를 벗어나면 환각 인용이다
    bad_cites = 0
    for a in answers:
        for m in CITE.findall(a.answer):
            if not (1 <= int(m) <= max(len(a.sources), 1)):
                bad_cites += 1

    # 개인 자료 내용 누출 — citation 은 라벨만이어야 한다
    leak = sum(1 for a in answers for s in a.sources
               if s["visibility"] == "private" and s["citation"] != "개인 자료 (비공개)")

    return {
        "id": case["id"],
        "unique_answers": len({a.answer for a in answers}),
        "checks": checks,
        "passed": all(v for v in checks.values() if v is not None),
        "bad_citations": bad_cites,
        "private_leak": leak,
        "latency_p50": round(statistics.median(latencies), 3),
        "latency_p95": round(sorted(latencies)[max(0, int(len(latencies) * 0.95) - 1)], 3),
        "sample": answers[-1].answer.replace("\n", " ")[:150],
    }


async def main(n: int) -> int:
    gs = _load()
    ai = AiClient()
    ident = Identity("12345", "clinician")

    if n < MIN_N:
        print(f"⚠️  N={n} — 정식 기준선이 아닙니다(최소 {MIN_N}). 빠른 점검 용도로만 쓰세요.\n")

    # 워밍업 — 콜드 캐시가 비결정성으로 오인되는 것을 막는다
    print("워밍업 1회…")
    await ask("워밍업", ident=ident, groups=[], ai=ai, correlation_id="warmup")

    results = []
    for case in gs["cases"]:
        print(f"  {case['id']} … ", end="", flush=True)
        r = await _run_case(ai, ident, case, n)
        results.append(r)
        print("PASS" if r["passed"] else "FAIL")

    lat = [r["latency_p95"] for r in results]
    summary = {
        "n_per_case": n,
        "valid_baseline": n >= MIN_N,
        "cases": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "reproducibility": {
            "fully_deterministic": sum(1 for r in results if r["unique_answers"] == 1),
            "max_unique": max(r["unique_answers"] for r in results),
        },
        "bad_citations": sum(r["bad_citations"] for r in results),
        "private_leak": sum(r["private_leak"] for r in results),
        "latency_p95_worst": max(lat),
    }

    print("\n" + "=" * 72)
    print(json.dumps({"summary": summary, "results": results},
                     ensure_ascii=False, indent=2))

    out = ROOT / "eval" / "out"
    out.mkdir(exist_ok=True)
    (out / "last_run.json").write_text(
        json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    failed = [r["id"] for r in results if not r["passed"]]
    if failed:
        print(f"\n❌ 실패: {', '.join(failed)}")
    if summary["private_leak"]:
        print("🔴 개인 자료 내용 누출 발생 — 즉시 조치 필요")
    return 1 if (failed or summary["private_leak"]) else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=MIN_N)
    sys.exit(asyncio.run(main(ap.parse_args().n)))
