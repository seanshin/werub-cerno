"""응답 가드 — 근거 강제·출처 표기·검토 플래그를 한 곳에서 조립한다.

01_구현계획.md §4 (7·10·11단계) · 백서 §8.

여기를 거치지 않고 응답을 만드는 경로를 두지 않는다. 근거 강제와 출처 표기가
호출부마다 흩어지면 언젠가 한 곳이 빠지고, 그 한 곳이 근거 없는 답변을 내보낸다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..ask.search import Evidence, SearchResult
from .dur import DurResult
from .highrisk import RiskSignal, detect, review_reasons

UNGROUNDED_ANSWER = "근거로 확인되지 않습니다."
"""근거 부족 시 **생성하지 않고** 이 문구로 응답한다. 모델에게 '모른다고 답해라'라고
지시하는 것과 다르다 — 생성 자체를 하지 않으므로 환각이 원리적으로 불가능하다."""


@dataclass
class Answer:
    answer: str
    grounded: bool
    sources: list[dict] = field(default_factory=list)
    needs_human_review: bool = False
    review_reasons: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "grounded": self.grounded,
            "sources": self.sources,
            "needs_human_review": self.needs_human_review,
            "review_reasons": self.review_reasons,
            "meta": self.meta,
        }


def _sources(evidence: list[Evidence]) -> list[dict]:
    """출처 표기는 **의무**다. 공개범위를 함께 실어 개인 자료임을 감추지 않는다."""
    return [
        {"citation": e.citation, "visibility": e.visibility,
         "confidence": round(e.confidence, 4)}
        for e in evidence
    ]


def ungrounded(search: SearchResult, *, meta: dict | None = None,
               dur: "DurResult | None" = None, query: str = "") -> Answer:
    """근거 미달 응답. 생성 경로를 타지 않는다.

    **안전 신호는 근거 유무와 독립**이다 — 근거 부족은 "답을 모른다"이지
    "안전하다"가 아니다. RAG 코퍼스에 문헌이 없어도:
      - DUR 정본 경고(식약처 임부금기 등)는 본문에 싣는다.
      - 질의의 고위험 신호(임신·용량·금기 등)는 탐지해 플래그한다.
    """
    body = UNGROUNDED_ANSWER
    reasons = ["근거 부족"]
    signals: list[RiskSignal] = detect(query) if query else []
    reasons.extend(review_reasons(signals))
    if search.degraded:
        reasons.append("일부 지식 저장소 조회 실패 — 결과가 불완전할 수 있습니다")
    if dur is not None and dur.needs_review:
        body = f"⚠️ {dur.summary()}\n\n{body}"
        reasons.extend(dur.reasons())
    return Answer(
        answer=body,
        grounded=False,
        sources=[],
        needs_human_review=True,   # 답을 못 준 것도 사람이 알아야 한다
        review_reasons=reasons + ([search.reason] if search.reason else []),
        meta={**(meta or {}), "degraded": search.degraded,
              "risk_kinds": [s.kind for s in signals],
              "dur_available": None if dur is None else dur.available},
    )


# 시스템 프롬프트가 지시한 회피 문구. 우리가 지시한 문구이므로 매칭은 휴리스틱이 아니라
# 프롬프트와의 계약이다(prompt_version 으로 고정된다).
_REFUSAL_MARKERS = ("근거에서 확인되지 않", "근거로 확인되지 않", "근거에 없")


def _looks_like_refusal(text: str) -> bool:
    head = text.strip()[:120]
    return any(m in head for m in _REFUSAL_MARKERS)


def finalize(
    generated: str,
    *,
    query: str,
    search: SearchResult,
    dur: DurResult | None = None,
    meta: dict | None = None,
) -> Answer:
    """생성 결과에 DUR 정본·출처·검토 플래그를 붙여 최종 응답으로 만든다.

    고위험 탐지는 **질의와 생성 답변을 함께** 본다 — 질의는 평이해도 답변에서 처음
    용량이 나올 수 있다.
    """
    body = generated.strip()

    # 🔴 DUR 정본은 **답변 본문에 실린다.** 검토 사유에만 두면 답변만 읽는 의료진이
    # 금기를 놓친다. 실제로 라이브 E2E 에서 "임신 중 이소트레티노인" 질의에 대해
    # 임부금기 3건이 검출됐는데 본문은 "근거에서 확인되지 않습니다" 였다.
    # DUR 은 식약처 정본이므로 RAG 근거 부족과 무관하게 전달해야 한다.
    # 조건을 직접 나열하지 않고 `needs_review` 를 쓴다 — 나열하면 새 상태가 생길 때마다
    # 여기를 같이 고쳐야 하고, 실제로 `meds_unavailable` 추가 때 한 번 빠뜨렸다.
    # 판단 기준은 DurResult 한 곳에만 둔다.
    if dur is not None and dur.needs_review:
        body = f"⚠️ {dur.summary()}\n\n{body}" if body else f"⚠️ {dur.summary()}"

    signals: list[RiskSignal] = detect(query, generated)
    reasons = review_reasons(signals)

    if search.degraded:
        reasons.append("일부 지식 저장소 조회 실패 — 결과가 불완전할 수 있습니다")
    if dur is not None:
        reasons.extend(dur.reasons())

    # 근거는 찾았으나 모델이 "근거에서 확인되지 않는다"고 답한 경우.
    # grounded=True 로 내보내면 소비자(EMR 표시 등)가 근거 있는 답으로 오인한다.
    refused = _looks_like_refusal(generated)
    if refused:
        reasons.append("검색된 근거가 질문에 답하지 못했습니다")

    return Answer(
        answer=body,
        grounded=not refused,
        sources=_sources(search.evidence),
        needs_human_review=bool(reasons),
        review_reasons=reasons,
        meta={
            **(meta or {}),
            "degraded": search.degraded,
            "risk_kinds": [s.kind for s in signals],
            "dur_available": None if dur is None else dur.available,
            "model_refused": refused,
        },
    )
