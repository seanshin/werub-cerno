"""충실도 표본 수집 버퍼 (17 기획 P0-3).

`faithfulness.py` 는 표집·제출 로직만 있고 표본이 흘러들어오는 경로가 없었다 —
13번 §5 의 "충실도 = AI 서버 야간 채점" 행이 0건 수집인 채로 파일럿이 시작됐다.
여기가 그 경로다: 파이프라인의 **생성 경로**(finalize)에서만 담는다. ungrounded
정형 문구는 생성이 없어 채점 대상이 아니다.

메모리 버퍼만 둔다 — 표본에는 질의·답변 원문이 있으므로 디스크에 남기지 않는다
(ask_log 승인 범위 밖). 재기동에 버퍼가 날아가는 것은 감수한다: 표본은 통계지
기록이 아니고, 다음 날 표본으로 채워진다.
"""
from __future__ import annotations

import threading
from collections import deque

from ..safety.guard import Answer
from .faithfulness import Sample

_MAX_BUFFER = 120
"""층화 목표 합(30)의 4배 — 하루치로 충분하고, 무한 적재를 막는다."""

_buf: deque[Sample] = deque(maxlen=_MAX_BUFFER)
_lock = threading.Lock()


def add_from(answer: Answer, *, question: str, evidence_texts: list[str],
             had_patient_context: bool) -> None:
    """생성된 답변 1건을 표본 후보로. 환자 맥락 답변은 **버퍼에도 넣지 않는다**
    (부분 근거 오염 — faithfulness.py 규율 1 — 에 더해, PHI 를 버퍼에조차 안 둔다)."""
    if had_patient_context:
        return
    s = Sample(
        correlation_id=str(answer.meta.get("correlation_id", "")),
        question=question,
        answer=answer.answer,
        evidence=evidence_texts,
        prompt_version=str(answer.meta.get("prompt_version", "")),
        grounded=answer.grounded,
        needs_review=answer.needs_human_review,
        refused=bool(answer.meta.get("model_refused")),
        had_patient_context=False,
    )
    with _lock:
        _buf.append(s)


def drain() -> list[Sample]:
    """제출용으로 전량 회수(버퍼 비움). 제출 실패 시 버리는 쪽을 택한다 —
    재적재하면 실패가 길어질 때 같은 표본이 늙은 채로 반복 제출된다."""
    with _lock:
        out = list(_buf)
        _buf.clear()
    return out


def pending() -> int:
    with _lock:
        return len(_buf)
