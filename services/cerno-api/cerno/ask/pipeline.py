"""질의 파이프라인 — 01_구현계획.md §4 의 11단계를 잇는다.

  1. 신원(호출부에서 검증 완료)
  2. 접근범위 계산
  3. (선택) FHIR 환자 컨텍스트
  4. 워밍
  5. 3컬렉션 병렬 검색
  6. 근거 게이트
  7. 근거 충분성 판정 → 미달이면 **생성 스킵**
  8. 개인화 프롬프트 구성
  9. 생성
 10. 후검증(고위험·DUR)
 11. 응답 조립

이 함수를 우회해 응답을 만드는 경로를 두지 않는다.
"""
from __future__ import annotations

import asyncio
import logging

from ..auth import Identity
from ..circuit import CircuitOpen
from ..clients.ai import AiBusy, AiClient, AiUnavailable
from ..eval import collector
from ..personal.prompt import PromptPersonalization
from ..safety import dur as dur_mod
from ..safety.guard import Answer, finalize, ungrounded
from .search import search_evidence

log = logging.getLogger("cerno.ask")

_personal = PromptPersonalization()

_warm_tasks: set[asyncio.Task] = set()
"""이벤트 루프는 태스크를 약참조만 하므로, 참조를 안 잡으면 진행 중에 GC 될 수 있다."""


def _fire_warm(ai: AiClient, clinician_id: str) -> None:
    t = asyncio.create_task(ai.warm(clinician_id=clinician_id))
    _warm_tasks.add(t)
    t.add_done_callback(_warm_tasks.discard)


async def ask(
    question: str,
    *,
    ident: Identity,
    groups: list[str],
    ai: AiClient,
    patient_context: str | None = None,
    drugs: list[str] | None = None,
    age: int | None = None,
    pregnant: bool | None = None,
    correlation_id: str = "",
    meds_unavailable: bool = False,
    history: list[tuple[str, str]] | None = None,
) -> Answer:
    """근거 기반 답변. 근거가 없으면 생성하지 않는다.

    `drugs` 가 주어지면 DUR 정본을 조회해 검토 사유에 반영한다. 환자 맥락에서 현재
    투약을 뽑아 넘기는 것은 호출부(FHIR 커넥터) 책임이다.
    """
    meta = {"clinician": ident.log_id, "correlation_id": correlation_id}

    # 4. 워밍 — 검색과 병렬로 던지고 기다리지 않는다. AI 서버가 상주 해제(요청 시 탑재·
    #    KEEP_ALIVE 5m) 운영이라 5분 유휴 후 첫 질의는 콜드 2.5~3.9초가 실재하는데,
    #    직렬 await 하면 그 전액이 응답에 가산된다. 생성(/api/chat)은 적재 완료를 서버가
    #    알아서 기다리므로 완료 동기화도 불필요하다. warm() 은 예외를 던지지 않는다.
    _fire_warm(ai, ident.clinician_id)

    # 5~7. 검색 → 게이트 → 충분성
    #
    # 이어서 질문은 단독으로 검색어가 빈약하다("그럼 용량은?"). 직전 **질문**을 검색어에
    # 합쳐 재현율을 올린다 — 답변은 합치지 않는다(길고, 모델 문장이 검색을 오염시킨다).
    # 근거 게이트는 그대로다: 합친 검색어로도 임계를 못 넘으면 생성하지 않는다.
    search_query = f"{history[-1][0]}\n{question}" if history else question
    try:
        search = await search_evidence(
            search_query, ident=ident, groups=groups, ai=ai,
            weights=_personal.weights(ident.clinician_id),
        )
    except AiBusy:
        raise
    except (AiUnavailable, CircuitOpen) as e:
        log.warning("검색 전면 실패 [%s]: %s", correlation_id, e)
        from .search import SearchResult
        return ungrounded(
            SearchResult(evidence=[], grounded=False, degraded=True,
                         reason="지식 저장소를 조회하지 못했습니다"),
            meta=meta, query=question)

    # DUR 은 **근거 게이트와 독립**이다. RAG 코퍼스에 해당 약물 문헌이 없어도
    # 식약처 정본 금기는 확인해야 한다 — "임신 중 이 약 되나요?" 에 근거 문서가
    # 없다고 임부금기를 놓치면 안 된다(하니스가 pregnancy-dur 로 잡은 결함).
    dur_res = None
    if drugs:
        dur_res = await dur_mod.check(drugs, clinician_id=ident.clinician_id, ai=ai,
                                      age=age, pregnant=pregnant)
    if meds_unavailable:
        dur_res = (dur_res or dur_mod.DurResult()).with_meds_unavailable()

    if not search.grounded:
        # 근거는 없지만 DUR 경고가 있으면 그것만이라도 전달한다.
        return ungrounded(search, meta=meta, dur=dur_res, query=question)

    # 8. 개인화 프롬프트
    p = _personal.apply(question, clinician_id=ident.clinician_id,
                        evidence=search.evidence, patient_context=patient_context)
    meta["prompt_version"] = p.prompt_version

    # 9. 생성 — 이어서 질문(history)은 선행 대화 턴으로만 주입한다.
    #    검색·근거 게이트·DUR 은 **현 질문만** 본다: 근거는 질문 단위 계약이고,
    #    이력이 게이트를 우회해 근거 없는 주제를 grounded 로 만들면 안 된다.
    msgs: list[dict] = [{"role": "system", "content": p.system}]
    for hq, ha in history or []:
        msgs.append({"role": "user", "content": hq})
        msgs.append({"role": "assistant", "content": ha})
    msgs.append({"role": "user", "content": p.prompt})
    if history:
        meta["history_turns"] = len(history)
    try:
        generated = await ai.chat(msgs, clinician_id=ident.clinician_id)
    except AiBusy:
        raise
    except (AiUnavailable, CircuitOpen) as e:
        log.warning("생성 실패 [%s]: %s", correlation_id, e)
        from .search import SearchResult
        return ungrounded(
            SearchResult(evidence=[], grounded=False, degraded=True,
                         reason="답변을 생성하지 못했습니다"),
            meta=meta, query=question)

    if not generated.strip():
        # 빈 응답을 통과시키면 "근거는 있는데 답이 없는" 상태가 grounded 로 나간다
        from .search import SearchResult
        return ungrounded(
            SearchResult(evidence=search.evidence, grounded=False,
                         reason="모델이 빈 응답을 반환했습니다"),
            meta=meta, query=question)

    # 10~11. 후검증·조립 (DUR 은 위에서 이미 계산됨)
    result = finalize(generated, query=question, search=search, dur=dur_res, meta=meta)

    # 충실도 표본 수집 (17 기획 P0-3) — 생성 경로에서만. 실패해도 답변은 나간다.
    try:
        collector.add_from(result, question=question,
                           evidence_texts=[e.text for e in search.evidence],
                           had_patient_context=bool(patient_context))
    except Exception:
        log.exception("충실도 표본 수집 실패 [%s]", correlation_id)
    return result
