"""충실도 표본 수집 버퍼 (17 기획 P0-3) — PHI 표본 미적재·회수 계약."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.eval import collector                 # noqa: E402
from cerno.eval.faithfulness import stratified   # noqa: E402
from cerno.safety.guard import Answer            # noqa: E402


def _answer(**meta) -> Answer:
    return Answer(answer="답변 [1]", grounded=True, sources=[],
                  meta={"correlation_id": "c1", "prompt_version": "v@1.1.0", **meta})


def _drain_all():
    collector.drain()


def test_patient_context_never_enters_buffer():
    _drain_all()
    collector.add_from(_answer(), question="q", evidence_texts=["e"],
                       had_patient_context=True)
    assert collector.pending() == 0              # 버퍼에조차 안 둔다


def test_collected_sample_maps_answer_fields():
    _drain_all()
    a = _answer(model_refused=True)
    a.needs_human_review = True
    collector.add_from(a, question="질문", evidence_texts=["근거1", "근거2"],
                       had_patient_context=False)
    [s] = collector.drain()
    assert (s.question, s.answer) == ("질문", "답변 [1]")
    assert s.evidence == ["근거1", "근거2"]
    assert s.refused and s.needs_review and s.grounded
    assert s.prompt_version == "v@1.1.0"
    assert s.stratum() == "refused"              # 층화가 기존 규칙대로 동작


def test_drain_empties_buffer():
    _drain_all()
    collector.add_from(_answer(), question="q", evidence_texts=[],
                       had_patient_context=False)
    assert collector.pending() == 1
    got = collector.drain()
    assert len(got) == 1 and collector.pending() == 0
    assert stratified(got)                       # 제출 경로에 그대로 태울 수 있다
