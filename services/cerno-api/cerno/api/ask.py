"""질의 API — `POST /ask`.

사양서 §6 계약. **`clinician_id` 는 본문에 없다** — 검증된 신원에서만 온다.
"""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from ..ask.log import AskLog, ReviewLog
from ..ask.pipeline import ask as run_ask
from ..auth import Identity, assert_no_clinician_override
from ..clients.ai import AiBusy
from ..fhir import EvidenceUnavailable, FhirClient
from ..fhir.context import build as build_context
from ..fhir.tokens import SmartReentryRequired
from ..obs import metrics
from .deps import get_ai, groups_of, require_identity

log = logging.getLogger("cerno.api.ask")

router = APIRouter(tags=["ask"])

_ask_log: AskLog | None = None


def get_ask_log() -> AskLog:
    global _ask_log
    if _ask_log is None:
        _ask_log = AskLog()
    return _ask_log


_review_log: ReviewLog | None = None


def get_review_log() -> ReviewLog:
    global _review_log
    if _review_log is None:
        _review_log = ReviewLog()
    return _review_log


class PatientContext(BaseModel):
    source: str = "emr"
    patient_id: str = Field("", description="FHIR Patient.id (MRN 아님)")


class HistoryTurn(BaseModel):
    """이어서 질문 — 직전 문답 1개. 서버는 저장하지 않는다(PHI 비저장 경계)."""
    query: str = Field(max_length=500)
    answer: str = Field(max_length=4000)


class AskIn(BaseModel):
    query: str
    specialty: str = ""
    patient_context: PatientContext | None = None
    drugs: list[str] = Field(default_factory=list,
                             description="DUR 점검 대상. 환자 맥락에서 추출해 넘긴다")
    age: int | None = None
    pregnant: bool | None = None
    history: list[HistoryTurn] = Field(
        default_factory=list,
        description="이어서 질문 — 최근 문답. 서버는 마지막 3개만 생성 문맥으로 쓴다")


@router.post("/ask")
async def ask_endpoint(
    body: AskIn, response: Response, ident: Identity = Depends(require_identity),
):
    # 불변 규칙 4 — 본문의 clinician_id 는 무시하고, 불일치면 거부
    try:
        assert_no_clinician_override(body.model_dump(), ident)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e)) from None

    if not body.query.strip():
        raise HTTPException(status_code=400, detail="query 가 비었습니다")

    cid = uuid.uuid4().hex[:12]
    response.headers["X-Correlation-Id"] = cid

    patient_text: str | None = None
    drugs = list(body.drugs)
    meds_unavailable = False

    if body.patient_context and body.patient_context.patient_id:
        try:
            fc = await FhirClient.for_clinician(ident.clinician_id)

            # 🔴 요청 환자 ≠ launch 환자면 거부한다.
            #
            # HIS 가 v2026-07-30 봉합에서 "검색 시 subject 를 토큰 환자로 **강제 주입**"
            # 하도록 바꿨다(거부가 아니라 주입). 즉 다른 환자 id 를 보내도 400 이 아니라
            # **토큰 환자의 데이터가 조용히 돌아온다.**
            # 그대로 두면 Cerno 가 환자 B 의 질문에 환자 A 의 맥락을 붙여 답하게 된다.
            # 화면에는 B 로 보이는데 근거는 A — 가장 위험한 종류의 혼동이다.
            want = body.patient_context.patient_id
            if fc.launch_patient and want != fc.launch_patient:
                log.warning("[%s] 요청 환자와 launch 환자 불일치 — 거부", cid)
                raise HTTPException(
                    status_code=409,
                    detail="요청한 환자가 현재 진료 컨텍스트와 다릅니다. "
                           "해당 환자 화면에서 다시 실행해 주십시오.")

            bundle = await fc.patient_context(want)
            ctx = build_context(bundle)
            patient_text = ctx.as_text() or None
            # 호출자가 약물을 명시하지 않았을 때만 FHIR 에서 채운다 — 명시값이 우선
            if not drugs:
                drugs = ctx.drugs
            meds_unavailable = not ctx.medications_available
            log.info("[%s] 환자 맥락 %d줄 · 투약 %d건 (meds_available=%s)",
                     cid, len(ctx.lines), len(ctx.drugs), ctx.medications_available)
        except SmartReentryRequired:
            raise HTTPException(status_code=401,
                                detail="SMART 재진입이 필요합니다") from None
        except EvidenceUnavailable as e:
            # 환자 맥락 없이 지식 근거만으로 진행한다. 조용히 넘기지 않고 표시한다.
            log.warning("[%s] 환자 맥락 조회 실패: %s", cid, e)
            meds_unavailable = True

    t0 = time.perf_counter()
    try:
        answer = await run_ask(
            body.query, ident=ident, groups=groups_of(ident), ai=get_ai(),
            patient_context=patient_text, drugs=drugs,
            age=body.age, pregnant=body.pregnant, correlation_id=cid,
            meds_unavailable=meds_unavailable,
            history=[(h.query, h.answer) for h in body.history[-3:]],
        )
    except AiBusy as e:
        metrics.record_busy()
        raise HTTPException(status_code=503, detail="AI 서버 혼잡",
                            headers={"Retry-After": str(int(e.retry_after))}) from None
    except SmartReentryRequired:
        raise HTTPException(status_code=401, detail="SMART 재진입이 필요합니다") from None

    metrics.record_ask(
        grounded=answer.grounded, needs_review=answer.needs_human_review,
        degraded=bool(answer.meta.get("degraded")),
        refused=bool(answer.meta.get("model_refused")),
        latency_s=time.perf_counter() - t0,
    )
    # 관측 — PHI·질의 원문을 남기지 않는다. 의료진 id 는 해시.
    log.info("[%s] clinician=%s grounded=%s review=%s sources=%d",
             cid, ident.log_id, answer.grounded, answer.needs_human_review,
             len(answer.sources))
    result = answer.to_dict()

    # 환자별 질의 보관(ask/log.py — PHI 경계의 명시적 예외, 본인만 재조회).
    # 보관 실패는 답변을 막지 않는다.
    if body.patient_context and body.patient_context.patient_id:
        try:
            get_ask_log().record(
                clinician_id=ident.clinician_id,
                patient_id=body.patient_context.patient_id,
                correlation_id=cid, query=body.query, answer=result)
        except Exception as e:  # noqa: BLE001
            log.warning("[%s] 질의 보관 실패(무시): %s", cid, type(e).__name__)
    return result


@router.get("/ask/history")
async def ask_history(patient_id: str, ident: Identity = Depends(require_identity)):
    """환자별 본인 질의 이력 — launch 재진입 시 기록 레일을 복원한다.

    격리: 검증된 신원의 clinician_id 로만 조회한다. 다른 의료진의 기록은
    쿼리 자체가 닿지 않는다(서고 private 와 같은 원칙).
    """
    if not patient_id.strip():
        raise HTTPException(status_code=400, detail="patient_id 가 비었습니다")
    entries = get_ask_log().for_patient(
        clinician_id=ident.clinician_id, patient_id=patient_id.strip())
    return {"entries": [
        {"query": e.query, "answer": e.answer,
         "correlation_id": e.correlation_id, "created_at": e.created_at}
        for e in entries]}


class SuggestIn(BaseModel):
    patient_context: PatientContext
    exclude: list[str] = Field(
        default_factory=list,
        description="이미 제시된 질문 — 추가 생성 시 중복을 피한다. 서버는 20개까지만 쓴다")
    focus: str = Field("", max_length=100,
                       description="확장 방향(topics 중 선택) — 이 주제에 집중해 생성")


_SUGGEST_CATEGORIES = ("병용·상호작용", "금기", "용량·목표수치", "모니터링", "기타")
_SUGGEST_RISKS = ("high", "medium", "low")

_SUGGEST_SYSTEM = (
    "당신은 임상 지식 질의 도우미다. 환자 요약을 보고, 의료진이 임상 지식 서고에 "
    "물을 만한 질문을 만든다. 진단이나 처방을 지시하지 않는다.")

_SUGGEST_INSTRUCTION = (
    "\n\n위 환자와 관련해 임상 지식 데이터베이스에 물을 만한 구체적 질문을 "
    "최대 5개 만들어라. JSON 객체로만 출력하라. 형식: "
    '{"questions": [{"q": "질문", "category": "병용·상호작용|금기|용량·목표수치|'
    '모니터링|기타", "risk": "high|medium|low", '
    '"basis": "이 질문의 근거가 된 환자 요약 항목 한 줄"}], '
    '"topics": ["이 환자에서 추가로 확장할 만한 질문 주제 3~6개"]} '
    "위험도는 놓쳤을 때 환자 위해 가능성이 큰 주제일수록 high 다. "
    "basis 는 환자 요약에 실제로 있는 항목만 인용하라. "
    "환자의 성별·나이에 부합하지 않는 주제는 제외하라 — 예: 남성 환자에게 임신·수유, "
    "성인에게 소아 용량. 환자 이름·식별번호는 쓰지 마라.")


def _parse_suggestions(raw: str) -> tuple[list[dict], list[str]]:
    """모델 출력에서 JSON 만 관대하게 추출·정규화 — 실패는 빈 목록(화면이 폴백).

    신형(객체 {questions, topics})·구형(배열)을 모두 받는다. category/risk 는
    허용값 밖이면 기본값으로 보정한다 — 추천은 최적화라, 모델의 형식 일탈로
    기능이 죽으면 안 된다.
    """
    import json as _json
    a = min((i for i in (raw.find("{"), raw.find("[")) if i >= 0), default=-1)
    b = max(raw.rfind("}"), raw.rfind("]"))
    if a < 0 or b <= a:
        return [], []
    try:
        data = _json.loads(raw[a:b + 1])
    except ValueError:
        return [], []
    if isinstance(data, dict):
        items = data.get("questions") or []
        topics = [str(t).strip()[:60] for t in (data.get("topics") or [])
                  if isinstance(t, str) and t.strip()][:6]
    else:
        items, topics = data, []
    out: list[dict] = []
    for it in items:
        if isinstance(it, str):
            q, cat, risk, basis = it, "기타", "medium", ""
        elif isinstance(it, dict):
            q = it.get("q") or it.get("question") or ""
            cat = it.get("category", "기타")
            risk = it.get("risk", "medium")
            basis = it.get("basis", "")
        else:
            continue
        q = str(q).strip()[:200]
        if not q:
            continue
        out.append({
            "question": q,
            "category": cat if cat in _SUGGEST_CATEGORIES else "기타",
            "risk": risk if risk in _SUGGEST_RISKS else "medium",
            "basis": str(basis).strip()[:200],
        })
    return out[:5], topics


@router.post("/suggest")
async def suggest_endpoint(body: SuggestIn, ident: Identity = Depends(require_identity)):
    """환자 맞춤 추천 질문 — launch 직후 화면이 부른다.

    환자 맥락으로 질문 후보를 최대 5개 생성한다. **저장·로깅하지 않는다**(질문에
    환자 상태가 스며들 수 있다 — PHI 비저장 경계는 /ask 와 동일). 실패는 전부
    빈 목록으로 수렴한다 — 추천은 최적화이지 정확성 요건이 아니다.
    """
    if not body.patient_context.patient_id:
        return {"questions": []}
    t0 = time.monotonic()
    try:
        fc = await FhirClient.for_clinician(ident.clinician_id)
        # /ask 와 동일한 불변 — 요청 환자 ≠ launch 환자면 거부(맥락 혼동 방지)
        want = body.patient_context.patient_id
        if fc.launch_patient and want != fc.launch_patient:
            raise HTTPException(
                status_code=409,
                detail="요청한 환자가 현재 진료 컨텍스트와 다릅니다.")
        bundle = await fc.patient_context(want)
        ctx = build_context(bundle)
        patient_text = ctx.as_text()
        summary = ctx.demographics    # "남성, 49세" — 배너용 (17 P1-7)
    except HTTPException:
        raise
    except SmartReentryRequired:
        raise HTTPException(status_code=401, detail="SMART 재진입이 필요합니다") from None
    except (EvidenceUnavailable, Exception) as e:  # noqa: BLE001
        log.info("추천 질문 — 환자 맥락 조회 실패(폴백): %s", type(e).__name__)
        metrics.record_suggest(questions=0)
        return {"questions": []}

    if not patient_text:
        metrics.record_suggest(questions=0)
        return {"questions": [], "topics": [], "patient_summary": summary}
    instruction = _SUGGEST_INSTRUCTION
    if body.focus:
        instruction += f"\n\n'{body.focus}' 주제에 집중해 질문을 만들어라."
    if body.exclude:
        seen = "\n".join(f"- {q[:200]}" for q in body.exclude[:20])
        instruction += f"\n\n다음 질문과 중복되지 않는 새로운 질문만 만들어라:\n{seen}"
    try:
        raw = await get_ai().chat(
            [{"role": "system", "content": _SUGGEST_SYSTEM},
             {"role": "user", "content": patient_text + instruction}],
            clinician_id=ident.clinician_id, num_predict=600,
        )
    except Exception as e:  # noqa: BLE001 — AiBusy 포함, 추천 실패는 무해해야 한다
        log.info("추천 질문 생성 실패(폴백): %s", type(e).__name__)
        metrics.record_suggest(questions=0, latency_s=time.monotonic() - t0)
        return {"questions": [], "topics": [], "patient_summary": summary}
    qs, topics = _parse_suggestions(raw)
    metrics.record_suggest(questions=len(qs), latency_s=time.monotonic() - t0)
    log.info("추천 질문 %d건·주제 %d건 clinician=%s",
             len(qs), len(topics), ident.log_id)  # 내용은 남기지 않는다
    return {"questions": qs, "topics": topics, "patient_summary": summary}


class ReviewIn(BaseModel):
    correlation_id: str = Field(min_length=1, max_length=64)
    usefulness: str = Field(description="helpful | harmless | misleading")
    citation_ok: bool | None = Field(None, description="출처 없는 답변이면 None")
    missing: bool = False
    missing_note: str = Field("", max_length=300)


@router.post("/review")
async def review_endpoint(body: ReviewIn, ident: Identity = Depends(require_identity)):
    """의료진 검수 접수 (13_파일럿_운영계획.md §4 · 14 기획 §4 · 17 기획 P0-1).

    **질의·답변 원문·환자 정보는 받지 않는다** — correlation_id 로 ask_log 와
    대조한다(PHI 비저장 경계). 기록은 review_log(SQLite) 가 정본 — 카운터·컨테이너
    로그는 재기동에 유실되므로 회고 원자료로 못 쓴다(D-0 실증). 저장 실패는
    **접수 실패로 반환**한다: 조용히 성공 처리하면 파일럿의 유일한 수집 행위가
    무음으로 새는 것이라, 여기서는 답변 경로(기록 실패 무시)와 반대로 기운다.
    """
    if body.usefulness not in ("helpful", "harmless", "misleading"):
        raise HTTPException(status_code=422, detail="usefulness 는 helpful|harmless|misleading")

    try:
        get_review_log().record(
            correlation_id=body.correlation_id, clinician_id=ident.clinician_id,
            usefulness=body.usefulness, citation_ok=body.citation_ok,
            missing=body.missing, missing_note=body.missing_note)
    except Exception:
        log.exception("검수 기록 저장 실패 [%s]", body.correlation_id)
        raise HTTPException(status_code=503, detail="검수 기록 저장 실패 — 다시 시도해 주세요") from None

    metrics.record_review(usefulness=body.usefulness,
                          citation_ok=body.citation_ok, missing=body.missing)
    # 오도·누락은 즉시 공유 대상(운영계획 §4) — WARNING 으로 층을 나눠 잡기 쉽게 한다.
    lvl = log.warning if body.usefulness == "misleading" or body.missing else log.info
    lvl("검수 [%s] clinician=%s usefulness=%s citation_ok=%s missing=%s note=%s",
        body.correlation_id, ident.log_id, body.usefulness,
        body.citation_ok, body.missing, body.missing_note or "-")
    return {"ok": True, "priority": body.usefulness == "misleading" or body.missing}


@router.post("/warm")
async def warm_endpoint(ident: Identity = Depends(require_identity)):
    """세션 진입(launch) 시 모델 예열.

    AI 서버가 상주 해제(요청 시 탑재·KEEP_ALIVE 5m) 운영이라, launch 직후 예열해 두면
    첫 질의가 콜드(2.5~3.9초)를 먹지 않는다. 워밍은 최적화이지 정확성 요건이 아니므로
    `AiClient.warm()` 은 실패를 삼키고, 이 응답은 항상 200 이다. 신원은 요구한다 —
    무인증 경로는 /health 뿐이라는 불변을 지키고, 익명 호출로 GPU 적재를 유발하지 못하게 한다.
    """
    await get_ai().warm(clinician_id=ident.clinician_id)
    return {"ok": True}
