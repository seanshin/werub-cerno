"""개인화 어댑터 — 의료진별 적용의 유일한 지점.

파이프라인은 개인화가 프롬프트로 이뤄지는지 어댑터로 이뤄지는지 몰라야 한다.
증설 + vLLM 전환 후 LoRA 를 붙일 때 **이 패키지만** 교체한다.
(01_구현계획.md §6.2)

현 전략은 'prompt' — Ollama 는 어댑터를 모델에 구워 배포하므로 의료진 전환이
전체 콜드 스왑이 된다. 무어댑터 개인화로 충분한지는 Phase 3 하니스가 측정하며,
그 수치가 LoRA 투자 판단의 근거다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Sequence

Strategy = Literal["prompt", "adapter"]


@dataclass(frozen=True)
class Evidence:
    """검색된 근거 1건. **정의는 여기 하나뿐이다** — `ask.search` 가 이것을 재수출한다.

    두 벌로 두었다가 `rank` 유무가 갈려 프롬프트 조립이 깨질 뻔했다(계약 테스트가 잡음).
    같은 개념을 두 곳에 정의하면 한쪽만 바뀐다.
    """

    doc_id: str
    text: str
    visibility: Literal["private", "group", "org"]
    confidence: float
    citation: str
    """org = 저자·게재지·DOI. private = '개인 자료(비공개)' — 내용·출처를 노출하지 않는다."""
    rank: int = 0
    """병합 후 순위. 프롬프트의 [n] 인용 번호와 응답 sources 순서가 이 값으로 맞춰진다."""


@dataclass(frozen=True)
class PersonalizedPrompt:
    system: str
    prompt: str
    prompt_version: str
    """예: 'cerno.narrative@1.0.0'. 응답 meta 에 실어 형상 추적을 확보한다.
    PATCH/MINOR/MAJOR ↔ 경미/영향평가/중요 변경 (식약처 PCCP 대응·twin 방식)."""
    adapter_id: str | None = None
    """strategy()=='adapter' 일 때만 채워진다. 현 구현은 항상 None."""


class PersonalizationBackend(ABC):
    @abstractmethod
    def strategy(self) -> Strategy: ...

    @abstractmethod
    def apply(
        self,
        question: str,
        *,
        clinician_id: str,
        evidence: Sequence[Evidence],
        patient_context: str | None = None,
    ) -> PersonalizedPrompt:
        """의료진 프로파일을 반영해 최종 프롬프트를 구성한다.

        'prompt' 전략의 구성 요소 (Phase 4-A):
          1. 의료진별 시스템 프롬프트 — 설명 방식·강조점·서술 순서
          2. few-shot — 개인 서고에서 검색한 본인의 과거 답변·노트
          3. 근거 — visibility 별로 구분해 배치, 개인 자료는 비공개 라벨

        근거가 비어 있으면 호출하지 않는다. 근거 부족 판정은 safety 가 담당하며
        생성 자체를 건너뛴다("근거로 확인되지 않음").
        """

    @abstractmethod
    def evidence_weight(self, *, clinician_id: str, visibility: str) -> float:
        """계층별 검색 가중치. 개인 서고를 우선 인용하게 하는 수단(Phase 4-A2)."""
