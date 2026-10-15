"""충실도 비동기 샘플링 — AI 서버 야간 배치에 답변을 태운다 (Phase 3-4).

AI 서버 회신3 §3: `POST /api/eval/pairs` 에 push. 접수만 하고 채점은 야간 cron(03:30).

설계 규율:
  1. **PHI 가 붙은 답변은 표본에서 제외한다.** AI 서버 경고(07-27 위양성): 맥락을 빼고
     지식 근거만 보내면 답변에는 맥락 기반 주장이 남고, 채점기가 그걸 "근거에 없다"로
     떨어뜨린다 — 정확한 답변인데도. 부분 근거는 측정을 오염시킨다.
     → 환자 컨텍스트가 붙은 답변(`had_patient_context=True`)은 아예 안 보낸다.
  2. **층화 표본**(grounded 20 · review 7 · refused 3). 무작위면 대부분 평범한 grounded 라
     정작 봐야 할 것(고위험·회피)이 안 잡힌다.
  3. **밀어 넣기**(push). Cerno 는 외부에서 접근되지 않는 구성이라 pull 은 경로가 없다.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from ..clients.ai import AiBusy, AiClient, AiUnavailable
from ..circuit import CircuitOpen

log = logging.getLogger("cerno.eval.faith")

# 층화 목표 — 합 30. AI 서버와 합의한 값.
STRATA = {"grounded": 20, "review": 7, "refused": 3}


@dataclass
class Sample:
    correlation_id: str
    question: str
    answer: str
    evidence: list[str]
    prompt_version: str
    grounded: bool
    needs_review: bool
    refused: bool
    had_patient_context: bool
    """True 면 표본에서 제외한다 — 부분 근거가 채점을 오염시킨다(AI 서버 07-27 위양성)."""

    def stratum(self) -> str | None:
        """이 표본이 속한 층. PHI 있으면 None(제외)."""
        if self.had_patient_context:
            return None
        if self.refused:
            return "refused"
        if self.needs_review:
            return "review"
        if self.grounded:
            return "grounded"
        return None

    def to_pair(self) -> dict:
        return {
            "id": self.correlation_id,
            "question": self.question,
            "answer": self.answer,
            "evidence": self.evidence,
            "meta": {"prompt_version": self.prompt_version,
                     "grounded": self.grounded,
                     "needs_human_review": self.needs_review},
        }


def stratified(samples: list[Sample], targets: dict[str, int] | None = None) -> list[dict]:
    """층별 상한까지 뽑아 pairs 로. PHI 표본은 stratum()이 None 이라 자동 제외된다.

    무작위성은 도입하지 않는다 — 스크립트가 결정적이어야 재현·디버깅이 된다.
    입력 순서(최근순 등)를 호출자가 정하고, 여기서는 앞에서부터 채운다.
    """
    targets = targets or STRATA
    counts: dict[str, int] = {k: 0 for k in targets}
    out: list[dict] = []
    for s in samples:
        st = s.stratum()
        if st is None or st not in targets:
            continue
        if counts[st] >= targets[st]:
            continue
        counts[st] += 1
        out.append(s.to_pair())
    return out


async def submit(ai: AiClient, clinician_id: str, samples: list[Sample]) -> dict:
    """야간 스풀에 밀어 넣는다. 한 건이 깨져도 나머지는 접수된다(accepted·rejected)."""
    pairs = stratified(samples)
    if not pairs:
        return {"submitted": 0, "reason": "표본 없음(전부 PHI 이거나 층 미달)"}
    try:
        r = await ai._post("/api/eval/pairs",
                           {"source": "cerno", "pairs": pairs}, clinician_id=clinician_id)
        log.info("충실도 표본 제출: accepted=%s rejected=%s",
                 r.get("accepted"), r.get("rejected"))
        return {"submitted": len(pairs), **r}
    except (AiBusy, AiUnavailable, CircuitOpen) as e:
        # 야간 배치라 사용자 대기가 없다. 실패해도 다음 회차에 다시 시도하면 된다.
        log.warning("충실도 제출 실패(다음 회차 재시도): %s", e)
        return {"submitted": 0, "error": str(e)[:120]}
