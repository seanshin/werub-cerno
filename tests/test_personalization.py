"""무어댑터 개인화 계약 테스트 (Phase 4-A).

핵심: 개인 취향이 **안전 규칙을 무력화하지 못한다** · few-shot 내용이 근거로 새지
않는다 · 프로파일이 바뀌면 prompt_version 이 따라 바뀐다(규제 추적성).
"""
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

import cerno.personal.prompt as pp  # noqa: E402
from cerno.personal import Evidence  # noqa: E402

ME = "12345"


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """의료진 프로파일을 임시 config 로 갈아끼운다."""
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "registry.yaml").write_text(textwrap.dedent("""
        base: cerno.narrative@1.0.0
        prompts:
          cerno.narrative@1.0.0:
            system: |
              근거에 있는 내용만으로 답한다. 없으면 확인되지 않는다고 명시한다.
    """), encoding="utf-8")

    def write(clinicians_yaml: str):
        (tmp_path / "clinicians.yaml").write_text(clinicians_yaml, encoding="utf-8")
        pp._registry.cache_clear()
        pp._clinicians.cache_clear()
        monkeypatch.setattr(pp, "_CONFIG_DIR", str(tmp_path))
        return pp.PromptPersonalization()

    monkeypatch.setattr(pp, "_CONFIG_DIR", str(tmp_path))
    yield write
    pp._registry.cache_clear()
    pp._clinicians.cache_clear()


def ev():
    return [Evidence("d1", "심부전 4제 병용", "org", 0.71, "심장학회 2025", 1)]


# ── 개인 스타일 ─────────────────────────────────────────────

def test_style_notes_appended_after_canonical_rules(cfg):
    """정본 안전 지시를 **덮어쓰지 않는다** — 개인 취향이 근거 강제를 무력화하면 안 된다."""
    p = cfg(textwrap.dedent("""
        defaults: {few_shot_k: 2}
        clinicians:
          - id: "12345"
            style_notes: 결론을 먼저 제시한다.
    """))
    out = p.apply("질문", clinician_id=ME, evidence=ev())
    sys_prompt = out.system
    assert sys_prompt.index("근거에 있는 내용만으로") < sys_prompt.index("결론을 먼저")


def test_no_profile_still_works(cfg):
    p = cfg("clinicians: []")
    out = p.apply("질문", clinician_id="unknown", evidence=ev())
    assert "근거에 있는 내용만으로" in out.system
    assert out.prompt_version.endswith("+p0")     # 프로파일 없음


# ── few-shot ────────────────────────────────────────────────

def test_few_shot_injected_with_do_not_use_as_fact_warning(cfg):
    p = cfg(textwrap.dedent("""
        defaults: {few_shot_k: 2}
        clinicians:
          - id: "12345"
            few_shot:
              - {q: "항응고 기준은?", a: "CHA2DS2-VASc 2점 이상.[1]"}
    """))
    out = p.apply("질문", clinician_id=ME, evidence=ev())
    assert "CHA2DS2-VASc" in out.prompt
    # 예시 내용이 근거로 오인되지 않도록 프롬프트가 명시해야 한다
    assert "내용을 사실로 쓰지 않는다" in out.prompt


def test_few_shot_capped_so_it_does_not_crowd_out_evidence(cfg):
    p = cfg(textwrap.dedent("""
        defaults: {few_shot_k: 1}
        clinicians:
          - id: "12345"
            few_shot:
              - {q: q1, a: a1}
              - {q: q2, a: a2}
              - {q: q3, a: a3}
    """))
    assert len(p.few_shot(ME)) == 1
    out = p.apply("질문", clinician_id=ME, evidence=ev())
    assert "a1" in out.prompt and "a2" not in out.prompt


def test_malformed_few_shot_entries_are_dropped(cfg):
    p = cfg(textwrap.dedent("""
        clinicians:
          - id: "12345"
            few_shot:
              - {q: 질문만 있음}
              - "문자열"
              - {q: 정상, a: 정상답변}
    """))
    assert p.few_shot(ME) == [{"q": "정상", "a": "정상답변"}]


def test_evidence_always_present_after_few_shot(cfg):
    """예시가 앞에 오더라도 근거가 질문 직전에 와야 모델이 근거를 우선한다."""
    p = cfg(textwrap.dedent("""
        clinicians:
          - id: "12345"
            few_shot: [{q: q1, a: a1}]
    """))
    out = p.apply("질문", clinician_id=ME, evidence=ev())
    assert out.prompt.index("[이 의료진의 답변 예시]") < out.prompt.index("[근거]")
    assert out.prompt.index("[근거]") < out.prompt.index("[질문]")


# ── 규제 추적성 — 프로파일이 바뀌면 버전이 바뀐다 ──────────

def test_profile_change_changes_prompt_version(cfg):
    """few-shot 도 프롬프트 내용이다. 바뀌었는데 버전이 같으면 '무엇이 보내졌나'를
    사후에 특정할 수 없다."""
    a = cfg('clinicians:\n  - {id: "12345", style_notes: "A"}').apply(
        "q", clinician_id=ME, evidence=ev()).prompt_version
    b = cfg('clinicians:\n  - {id: "12345", style_notes: "B"}').apply(
        "q", clinician_id=ME, evidence=ev()).prompt_version
    assert a != b
    assert a.startswith("cerno.narrative@1.0.0+p")


def test_same_profile_is_stable(cfg):
    y = 'clinicians:\n  - {id: "12345", style_notes: "A"}'
    a = cfg(y).apply("q", clinician_id=ME, evidence=ev()).prompt_version
    b = cfg(y).apply("q", clinician_id=ME, evidence=ev()).prompt_version
    assert a == b


# ── 개인 서고 가중 ──────────────────────────────────────────

def test_default_weights_favour_private_shelf(cfg):
    p = cfg("clinicians: []")
    w = p.weights(ME)
    assert w["private"] > w["group"] > w["org"]


def test_per_clinician_weight_override(cfg):
    p = cfg(textwrap.dedent("""
        clinicians:
          - id: "12345"
            evidence_weight: {private: 1.0}
    """))
    assert p.weights(ME)["private"] == 1.0


def test_strategy_is_prompt_and_no_adapter(cfg):
    """증설·vLLM 전환 전까지는 어댑터 경로가 열리면 안 된다."""
    p = cfg("clinicians: []")
    assert p.strategy() == "prompt"
    assert p.apply("q", clinician_id=ME, evidence=ev()).adapter_id is None
