"""안전 가드레일 계약 테스트 (Phase 3-1·3-2·3-3).

DUR 부분은 **라이브 실호출로 확인한 계약**을 고정한다 — 문서만 보고 짰으면
빠졌을 함정 두 개(count 신뢰 / 미해석을 안전으로 오독)를 회귀로 막는다.
"""
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.ask.search import Evidence, SearchResult
from cerno.circuit import breaker
from cerno.clients.ai import AiClient, KeyResolver
from cerno.safety.dur import DurResult, check as dur_check
from cerno.safety.guard import UNGROUNDED_ANSWER, finalize, ungrounded
from cerno.safety.highrisk import detect

ME = "12345"


def ai(handler) -> AiClient:
    return AiClient(base_url="https://ai.example",
                    keys=KeyResolver(mapping={ME: "sk-a"}),
                    transport=httpx.MockTransport(handler))


def grounded(*, degraded=False) -> SearchResult:
    return SearchResult(
        evidence=[Evidence("d1", "본문", "org", 0.71, "대한심장학회 2025", 1),
                  Evidence("p1", "내 노트", "private", 0.66, "개인 자료 (비공개)", 2)],
        grounded=True, degraded=degraded)


@pytest.fixture(autouse=True)
def _reset():
    breaker("ai").record_success()
    yield
    breaker("ai").record_success()


# ── 고위험 탐지 ─────────────────────────────────────────────

@pytest.mark.parametrize("text,kind", [
    ("성인 용량은 10mg 1일 1회입니다", "dosage"),
    ("증량은 언제 하나요", "dosage"),
    ("아나필락시스 쇼크 초기 처치", "emergency"),
    ("와파린과 아스피린 병용해도 되나", "contraindication"),
    ("소아 체중당 용량", "pediatric"),
    ("임신 중 복용 가능한가", "pregnancy"),
    ("항암 화학요법 중 발열", "oncology"),
])
def test_high_risk_signals_detected(text, kind):
    assert kind in [s.kind for s in detect(text)]


def test_plain_question_is_not_flagged():
    assert detect("심부전의 정의가 무엇인가요") == []


def test_answer_text_is_scanned_not_only_query():
    """질의는 평이해도 답변에서 처음 용량이 나올 수 있다."""
    signals = detect("심부전 약제는?", "SGLT2 억제제 10mg 1일 1회를 사용합니다")
    assert "dosage" in [s.kind for s in signals]


# ── 🔴 DUR 함정 1: count 는 경고 수가 아니다 ────────────────

@pytest.mark.asyncio
async def test_pregnancy_warning_found_even_when_count_is_zero():
    """실측: 이소트레티노인+임부 → pregnancy_warnings 3건인데 count=0.
    count 로 분기하면 임부 금기를 놓친다."""
    def h(req):
        return httpx.Response(200, json={
            "count": 0,
            "pregnancy_warnings": [{"ingr_name": "이소트레티노인", "criteria": "1등급"}],
            "contraindications": [], "elderly_warnings": [], "age_warnings": [],
            "duplicate_therapy": [], "split_cautions": [], "advisories": [],
            "resolved": [{"input": {"name": "이소트레티노인"}, "resolved": True}],
        })

    r = await dur_check(["이소트레티노인"], clinician_id=ME, ai=ai(h), pregnant=True)
    assert r.total == 1
    assert r.needs_review is True
    assert any("임부금기" in x for x in r.reasons())


# ── 🔴 DUR 함정 2: 미해석은 "안전"이 아니라 "모름" ──────────

@pytest.mark.asyncio
async def test_unresolved_drug_is_not_reported_as_safe():
    """가장 위험한 오답 — 인식 못 한 약을 '금기 없음'으로 읽는 것."""
    def h(req):
        return httpx.Response(200, json={
            "count": 0, "contraindications": [], "pregnancy_warnings": [],
            "elderly_warnings": [], "age_warnings": [], "duplicate_therapy": [],
            "split_cautions": [], "advisories": [],
            "resolved": [{"input": {"name": "존재하지않는약"}, "resolved": False}],
        })

    r = await dur_check(["존재하지않는약"], clinician_id=ME, ai=ai(h))
    assert r.total == 0
    assert r.unresolved == ["존재하지않는약"]
    assert r.needs_review is True
    assert any("확인 불가" in x for x in r.reasons())
    assert "금기가 없습니다" not in r.summary()


@pytest.mark.asyncio
async def test_truly_clean_result_says_so():
    def h(req):
        return httpx.Response(200, json={
            "count": 0, "contraindications": [], "pregnancy_warnings": [],
            "elderly_warnings": [], "age_warnings": [], "duplicate_therapy": [],
            "split_cautions": [], "advisories": [],
            "resolved": [{"input": {"name": "아세트아미노펜"}, "resolved": True}],
        })

    r = await dur_check(["아세트아미노펜"], clinician_id=ME, ai=ai(h))
    assert r.needs_review is False
    assert "확인된 DUR 금기·주의가 없습니다" in r.summary()


@pytest.mark.asyncio
async def test_dur_failure_is_a_value_not_an_exception():
    """예외로 올리면 호출부가 try 를 빠뜨렸을 때 '금기 없음' 경로로 흐른다."""
    def h(req):
        return httpx.Response(500)

    r = await dur_check(["와파린"], clinician_id=ME, ai=ai(h))
    assert r.available is False
    assert r.needs_review is True
    assert "실패" in r.summary()
    assert "금기가 없습니다" not in r.summary(), "조회 실패를 안전으로 읽히게 하면 안 된다"


# ── 근거 강제 ───────────────────────────────────────────────

def test_ungrounded_does_not_generate():
    a = ungrounded(SearchResult(evidence=[], grounded=False, reason="임계 미달"))
    assert a.answer == UNGROUNDED_ANSWER
    assert a.grounded is False
    assert a.sources == []
    assert a.needs_human_review is True


def test_degraded_search_is_disclosed():
    a = ungrounded(SearchResult(evidence=[], grounded=False, degraded=True))
    assert any("조회 실패" in r for r in a.review_reasons)


# ── 출처 표기는 의무 ────────────────────────────────────────

def test_every_source_carries_visibility():
    a = finalize("답변", query="질문", search=grounded())
    assert len(a.sources) == 2
    assert {s["visibility"] for s in a.sources} == {"org", "private"}


def test_private_source_content_is_not_exposed():
    a = finalize("답변", query="질문", search=grounded())
    priv = [s for s in a.sources if s["visibility"] == "private"][0]
    assert priv["citation"] == "개인 자료 (비공개)"
    assert "노트" not in priv["citation"]


# ── 검토 플래그 조립 ────────────────────────────────────────

def test_plain_grounded_answer_needs_no_review():
    a = finalize("심부전은 심장의 펌프 기능이 저하된 상태입니다.",
                 query="심부전이란", search=grounded())
    assert a.needs_human_review is False
    assert a.review_reasons == []


def test_dosage_in_answer_triggers_review():
    a = finalize("SGLT2 억제제 10mg 1일 1회를 사용합니다.",
                 query="심부전 약제는", search=grounded())
    assert a.needs_human_review is True
    assert "dosage" in a.meta["risk_kinds"]


def test_dur_warning_propagates_to_review_reasons():
    dur = DurResult(warnings={"pregnancy_warnings": [{"x": 1}]}, unresolved=[], available=True)
    a = finalize("답변", query="질문", search=grounded(), dur=dur)
    assert a.needs_human_review is True
    assert any("임부금기" in r for r in a.review_reasons)


def test_dur_unavailable_propagates():
    a = finalize("답변", query="질문", search=grounded(), dur=DurResult(available=False))
    assert a.needs_human_review is True
    assert a.meta["dur_available"] is False


# ── 🔴 라이브 E2E 에서 드러난 것 (2026-07-30) ───────────────

def test_dur_warning_appears_in_answer_body_not_only_flags():
    """임부금기 3건인데 본문이 '근거에서 확인되지 않습니다' 였다.
    답변만 읽는 의료진이 금기를 놓친다 — DUR 은 식약처 정본이므로 본문에 싣는다."""
    dur = DurResult(warnings={"pregnancy_warnings": [{"a": 1}, {"b": 2}, {"c": 3}]},
                    available=True)
    a = finalize("근거에서 확인되지 않습니다.", query="임신 중 이소트레티노인 복용",
                 search=grounded(), dur=dur)
    assert "임부금기" in a.answer, "DUR 경고가 본문에 없다"
    assert a.answer.startswith("⚠️")


def test_dur_unavailable_also_surfaces_in_body():
    a = finalize("답변", query="질문", search=grounded(), dur=DurResult(available=False))
    assert "실패" in a.answer


def test_clean_dur_does_not_clutter_the_answer():
    dur = DurResult(warnings={k: [] for k in ("contraindications",)}, available=True)
    a = finalize("답변 본문", query="질문", search=grounded(), dur=dur)
    assert a.answer == "답변 본문"


def test_model_refusal_downgrades_grounded():
    """근거는 찾았으나 모델이 답하지 못한 경우, grounded=True 로 내보내면
    소비자가 근거 있는 답으로 오인한다."""
    a = finalize("근거에서 확인되지 않습니다.", query="질문", search=grounded())
    assert a.grounded is False
    assert a.meta["model_refused"] is True
    assert any("답하지 못했" in r for r in a.review_reasons)


def test_normal_answer_stays_grounded():
    a = finalize("제2형 당뇨병 목표는 7% 미만입니다.[1]", query="목표치", search=grounded())
    assert a.grounded is True
    assert a.meta["model_refused"] is False


# ── 🔴 안전 신호는 근거 게이트와 독립 (하니스 pregnancy-dur 발견) ──

def test_dur_surfaces_even_when_ungrounded():
    """근거 부족은 '답을 모른다'이지 '안전하다'가 아니다. RAG 코퍼스에 약물 문헌이
    없어도 식약처 임부금기는 전달해야 한다."""
    dur = DurResult(warnings={"pregnancy_warnings": [{"a": 1}, {"b": 2}, {"c": 3}]}, available=True)
    a = ungrounded(SearchResult(evidence=[], grounded=False, reason="임계 미달"),
                   dur=dur, query="임신 중 이소트레티노인 복용")
    assert "임부금기" in a.answer, "근거 없다고 DUR 경고를 빠뜨렸다"
    assert a.grounded is False


def test_high_risk_detected_even_when_ungrounded():
    """근거 없어도 질의가 고위험이면 플래그한다."""
    a = ungrounded(SearchResult(evidence=[], grounded=False, reason="x"),
                   query="임신 중 이 약 복용해도 되나요")
    assert "pregnancy" in a.meta["risk_kinds"]
    assert a.needs_human_review is True


def test_ungrounded_without_dur_or_risk_is_plain():
    a = ungrounded(SearchResult(evidence=[], grounded=False, reason="x"),
                   query="심부전의 정의는")
    assert a.answer == UNGROUNDED_ANSWER
    assert a.meta["risk_kinds"] == []
