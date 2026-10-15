"""고위험 신호 탐지 — 사람 검토를 유도할 질의를 가려낸다 (백서 §8 · 사양서 §8).

**차단이 아니라 플래그다.** Cerno 는 보조 도구이고 최종 판단은 의료진에게 있으므로,
답을 막는 것이 아니라 "이 답은 사람이 확인해야 한다"를 응답에 실어 보낸다.

설계 원칙: **과탐이 미탐보다 낫다.** 불필요한 검토 플래그는 성가심이지만, 놓친
고위험 답변은 환자에게 간다. 애매하면 플래그를 세운다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

RiskKind = Literal["dosage", "emergency", "contraindication", "pediatric", "pregnancy", "oncology"]

# 용량 — 숫자+단위, 또는 증감량 의도. "하루 2회" 같은 빈도도 포함한다.
_DOSAGE = re.compile(
    r"\d+\s*(?:mg|mcg|µg|ug|g|ml|mL|IU|단위|정|캡슐|앰플|바이알)\b"
    r"|용량|용법|증량|감량|투여량|투약량|몇\s*(?:mg|정|알)|최대\s*용량|1일\s*\d|하루\s*\d"
)
_EMERGENCY = re.compile(
    r"응급|급성|쇼크|심정지|심폐소생|아나필락시스|과민성\s*쇼크|대량\s*출혈|의식\s*(?:저하|소실)"
    r"|경련\s*중첩|패혈증|호흡\s*부전|기도\s*확보|중환자|ICU|소생술|STEMI|뇌졸중\s*골든"
)
_CONTRA = re.compile(
    r"금기|병용\s*금기|병용|상호작용|중복\s*투여|같이\s*(?:먹|복용|투여|처방)"
    r"|함께\s*(?:먹|복용|투여|처방)|DUR"
)
_PEDIATRIC = re.compile(r"소아|신생아|영아|유아|미숙아|어린이|체중\s*당|kg\s*당|mg/kg")
_PREGNANCY = re.compile(r"임신|임부|임산부|수유|모유|태아|기형|가임")
_ONCOLOGY = re.compile(r"항암|화학요법|세포독성|방사선\s*치료|표적\s*치료|면역\s*항암")

_PATTERNS: list[tuple[RiskKind, re.Pattern[str], str]] = [
    ("dosage", _DOSAGE, "용량·용법 관련 질의"),
    ("emergency", _EMERGENCY, "응급·중증 상황 관련 질의"),
    ("contraindication", _CONTRA, "금기·병용·상호작용 관련 질의"),
    ("pediatric", _PEDIATRIC, "소아 대상 질의"),
    ("pregnancy", _PREGNANCY, "임신·수유 관련 질의"),
    ("oncology", _ONCOLOGY, "항암요법 관련 질의"),
]


@dataclass(frozen=True)
class RiskSignal:
    kind: RiskKind
    reason: str


def detect(*texts: str) -> list[RiskSignal]:
    """질의와 (있으면) 생성 답변을 함께 넣는다.

    답변까지 보는 이유: 질의는 평이해도 답변이 용량을 제시할 수 있다. 예컨대
    "심부전 약제는?" → "SGLT2 억제제 10mg 1일 1회" 처럼 답에서 처음 용량이 나온다.
    """
    blob = "\n".join(t for t in texts if t)
    if not blob:
        return []
    return [RiskSignal(kind, reason) for kind, pat, reason in _PATTERNS if pat.search(blob)]


def review_reasons(signals: list[RiskSignal]) -> list[str]:
    return [s.reason for s in signals]
