"""프롬프트 인젝션 방어 — 데이터가 프롬프트 구조를 위조하지 못하게 한다.

근거 텍스트·인용·질의·환자 맥락은 전부 **신뢰할 수 없는 입력**이다.
  - 근거·인용: 의료진이 올린 문서. org 컬렉션은 전 의료진 공유라 한 명의 악성 문서가
    모두의 답변을 오염시킬 수 있다.
  - 질의: 사용자 입력.
  - 환자 맥락: FHIR 자유텍스트(DocumentReference 서술 등).

이 값들이 프롬프트 섹션 헤더(`[근거]`·`[질문]` 등)를 흉내내면 블록 경계를 위조해
"위 지시를 무시하고…" 같은 주입이 가능하다(라이브 재현됨).

완벽한 인젝션 방어는 없다. 여기서는 **구조 위조를 차단**하고(데이터가 우리 섹션
헤더를 만들 수 없게), 나머지는 시스템 프롬프트의 데이터/지시 경계 규칙 + 기존
방어(근거 부족 시 생성 안 함·출처 표기·회피 시 grounded 강등)로 심층 방어한다.

정상 의료 텍스트를 깨뜨리지 않는 게 관건이다 — `[1]` 참고문헌 인용은 흔하므로
**우리 정확한 섹션 헤더만** 타깃한다.
"""
from __future__ import annotations

import re

# 프롬프트 조립에 쓰는 섹션 헤더 전량(prompt.py 와 일치해야 한다).
SECTION_HEADERS = (
    "[이 의료진의 답변 예시]",
    "[환자 맥락]",
    "[근거]",
    "[질문]",
)

# 전각 대괄호로 치환 — 시각적으로 거의 같아 사람은 읽을 수 있으나, 우리 파서/구조와는
# 다른 문자라 섹션 경계로 오인되지 않는다.
_FULLWIDTH = {"[": "［", "]": "］"}


def _neutralize_headers(text: str) -> str:
    """데이터에 우리 섹션 헤더가 그대로 있으면 대괄호를 전각으로 바꿔 무력화."""
    out = text
    for h in SECTION_HEADERS:
        if h in out:
            fw = "".join(_FULLWIDTH.get(c, c) for c in h)
            out = out.replace(h, fw)
    return out


# 라인 시작에서 우리 헤더를 흉내내는 변형(공백 포함 `[ 근거 ]` 등)도 잡는다.
_HEADER_LIKE = re.compile(
    r"(?m)^\s*\[\s*(?:이\s*의료진의\s*답변\s*예시|환자\s*맥락|근거|질문)\s*\]")


def sanitize_data(text: str | None) -> str:
    """프롬프트에 넣을 **데이터** 필드를 정화한다. 지시가 아니라 데이터임을 보장한다.

    - 정확한 섹션 헤더 → 전각 무력화
    - 라인 시작의 헤더 유사 패턴 → 대괄호 전각화
    - 널·제어문자 제거(프롬프트 파서 교란 방지)
    반환은 항상 문자열(None → "").
    """
    if not text:
        return ""
    t = _neutralize_headers(str(text))
    t = _HEADER_LIKE.sub(lambda m: m.group(0).replace("[", "［").replace("]", "］"), t)
    # 널·대부분의 C0 제어문자 제거(개행·탭은 유지 — 임상 텍스트 서식)
    t = "".join(ch for ch in t if ch == "\n" or ch == "\t" or ord(ch) >= 0x20)
    return t


def has_injection_markers(text: str | None) -> bool:
    """관측·테스트용 — 데이터에 구조 위조 시도가 있었는지."""
    if not text:
        return False
    return any(h in text for h in SECTION_HEADERS) or bool(_HEADER_LIKE.search(text))
