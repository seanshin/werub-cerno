"""DUR 연계 — AI 서버 식약처 정본(`/api/medical/dur/check`).

계약은 **라이브 실호출로 확인했다**(2026-07-30). 문서만 보고 짰으면 함정 두 개에
그대로 빠졌을 것이다.

🔴 함정 1 — `count` 는 경고 수가 아니다.
   이소트레티노인 + pregnant=true 실측: `pregnancy_warnings` 3건인데 **`count`=0**.
   `count` 로 분기하면 임부 금기를 놓친다. **실제 리스트를 합산해야 한다.**

🔴 함정 2 — 미해석 약물의 빈 경고는 "안전"이 아니라 "모름"이다.
   실측: `{"drugs":["존재하지않는약"]}` → 전 경고 리스트가 비고 `resolved[0].resolved=false`.
   이걸 "금기 없음"으로 읽으면 **가장 위험한 오답**이 된다. 미해석은 반드시
   사람 검토로 올린다.

경고 필드가 6종으로 나뉘어 있다는 점도 실측으로 확인했다 — 임부 금기는
`contraindications` 가 아니라 `pregnancy_warnings` 다(AI 서버도 같은 오독 이력이 있다).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..circuit import CircuitOpen
from ..clients.ai import AiBusy, AiClient, AiUnavailable

log = logging.getLogger("cerno.safety.dur")

# 실측으로 확인한 경고 리스트 필드 전량. 새 필드가 생겨도 count 에 의존하지 않도록
# 여기에 명시적으로 나열한다.
WARNING_FIELDS: tuple[str, ...] = (
    "contraindications",     # 병용금기
    "pregnancy_warnings",    # 임부금기
    "elderly_warnings",      # 노인주의
    "age_warnings",          # 연령금기
    "duplicate_therapy",     # 효능군 중복
    "split_cautions",        # 서방정 분할주의
    "advisories",
)

_LABEL = {
    "contraindications": "병용금기",
    "pregnancy_warnings": "임부금기",
    "elderly_warnings": "노인주의",
    "age_warnings": "연령금기",
    "duplicate_therapy": "효능군중복",
    "split_cautions": "서방정분할주의",
    "advisories": "주의",
}


@dataclass(frozen=True)
class DurResult:
    warnings: dict[str, list[dict]] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    """DUR 성분으로 인식되지 않은 약물. 비어 있지 않으면 결과가 불완전하다."""
    available: bool = True
    """False = DUR 조회 자체가 실패. '금기 없음'과 구분해야 한다."""
    meds_unavailable: bool = False
    """현재 투약 목록을 HIS 에서 읽지 못했다. 점검 대상이 불완전하므로 결과가
    깨끗해도 '금기 없음' 이 아니다. HIS 가드 교체 전에는 상시 True 다."""

    def with_meds_unavailable(self) -> "DurResult":
        return DurResult(warnings=self.warnings, unresolved=self.unresolved,
                         available=self.available, meds_unavailable=True)

    @property
    def total(self) -> int:
        return sum(len(v) for v in self.warnings.values())

    @property
    def needs_review(self) -> bool:
        """경고가 있거나, 미해석이 있거나, 조회가 실패하면 사람이 봐야 한다."""
        return bool(self.total or self.unresolved or not self.available
                    or self.meds_unavailable)

    def reasons(self) -> list[str]:
        out: list[str] = []
        for f, items in self.warnings.items():
            if items:
                out.append(f"DUR {_LABEL.get(f, f)} {len(items)}건")
        if self.unresolved:
            out.append(
                "DUR 성분으로 인식되지 않은 약물: " + ", ".join(self.unresolved)
                + " — 금기 없음이 아니라 확인 불가입니다"
            )
        if not self.available:
            out.append("DUR 조회 실패 — 금기 여부를 확인하지 못했습니다")
        if self.meds_unavailable:
            out.append("현재 투약 목록을 조회하지 못해 DUR 점검이 불완전합니다")
        return out

    def summary(self) -> str:
        if not self.available:
            return "DUR 조회에 실패했습니다. 금기 여부는 별도로 확인해 주십시오."
        parts = [f"{_LABEL.get(f, f)} {len(v)}건" for f, v in self.warnings.items() if v]
        if self.unresolved:
            parts.append(f"미인식 {len(self.unresolved)}건")
        if self.meds_unavailable:
            head = ("현재 투약 목록을 조회하지 못했습니다 — DUR 점검이 불완전합니다. "
                    "금기 여부를 별도로 확인해 주십시오.")
            return f"{head} ({' · '.join(parts)})" if parts else head
        if not parts:
            return "확인된 DUR 금기·주의가 없습니다 (식약처 정본)."
        return "DUR — " + " · ".join(parts) + " (식약처 정본, 최종 판단은 처방의·약사)"


async def check(
    drugs: list[str],
    *,
    clinician_id: str,
    ai: AiClient,
    age: int | None = None,
    pregnant: bool | None = None,
) -> DurResult:
    """실패해도 예외를 던지지 않는다 — `available=False` 로 돌려 상위가 플래그하게 한다.

    DUR 조회 실패를 예외로 올리면 호출부가 try/except 를 빠뜨렸을 때 조용히 '금기 없음'
    경로로 흐를 수 있다. 실패를 값으로 표현해 그 사고를 구조적으로 막는다.
    """
    if not drugs:
        return DurResult()

    payload: dict = {"drugs": drugs}
    if age is not None:
        payload["age"] = age
    if pregnant is not None:
        payload["pregnant"] = pregnant

    try:
        data = await ai._post("/api/medical/dur/check", payload, clinician_id=clinician_id)
    except (AiBusy, AiUnavailable, CircuitOpen) as e:
        log.warning("DUR 조회 실패: %s", e)
        return DurResult(available=False)

    warnings = {f: list(data.get(f) or []) for f in WARNING_FIELDS}
    unresolved = [
        str((r.get("input") or {}).get("name") or "?")
        for r in (data.get("resolved") or [])
        if not r.get("resolved")
    ]
    return DurResult(warnings=warnings, unresolved=unresolved, available=True)
