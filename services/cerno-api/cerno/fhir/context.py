"""FHIR 번들 → 프롬프트용 환자 맥락 + DUR 대상 약물 추출.

**최소 수집 원칙을 코드로 강제한다.** 직접 식별자(성명·주민번호·연락처·주소)는
애초에 꺼내지 않는다 — 마스킹은 빠뜨릴 수 있지만, 꺼내지 않은 값은 샐 수 없다.

이 모듈이 안전 루프를 닫는다:
    HIS MedicationRequest → 성분/약품명 추출 → AI 서버 DUR 정본 점검 → 검토 플래그
HIS 가드 교체(요청서 §A) 전에는 MedicationRequest 가 401/403 이라 약물 목록이 비고,
그러면 DUR 은 "점검할 약물 없음" 이 된다. **그 상태를 '금기 없음' 으로 읽지 않도록**
`medications_available` 로 구분해 올린다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# 프롬프트에 넣을 리소스별 상한. 컨텍스트를 넘치게 하면 근거가 밀려난다
# (OLLAMA_CONTEXT_LENGTH=8192, NUM_PARALLEL=2 슬롯 공유).
_LIMITS = {"Condition": 8, "Observation": 10, "MedicationRequest": 15,
           "AllergyIntolerance": 8, "DiagnosticReport": 3, "Procedure": 5}


@dataclass
class PatientContext:
    lines: list[str] = field(default_factory=list)
    drugs: list[str] = field(default_factory=list)
    medications_available: bool = False
    """MedicationRequest 를 실제로 읽었는가. False 면 DUR 결과를 '금기 없음' 으로
    해석하면 안 된다 — 조회가 막혀 있었을 뿐일 수 있다."""
    allergies_available: bool = False
    demographics: str = ""
    """"남성, 49세" — 화면 배너용 (17 P1-7). 이름·식별번호는 처음부터 수집 안 함."""

    def as_text(self) -> str:
        return "\n".join(self.lines)

    @property
    def empty(self) -> bool:
        return not self.lines


def _text(node: dict | None) -> str:
    """CodeableConcept → 사람이 읽는 문자열. text 우선, 없으면 coding.display."""
    if not isinstance(node, dict):
        return ""
    if node.get("text"):
        return str(node["text"]).strip()
    for c in node.get("coding") or []:
        if isinstance(c, dict) and c.get("display"):
            return str(c["display"]).strip()
    for c in node.get("coding") or []:
        if isinstance(c, dict) and c.get("code"):
            return str(c["code"]).strip()
    return ""


def _med_name(r: dict) -> str:
    """MedicationRequest 에서 약품·성분명. 참조형(medicationReference)은 display 만."""
    n = _text(r.get("medicationCodeableConcept"))
    if n:
        return n
    ref = r.get("medicationReference")
    if isinstance(ref, dict) and ref.get("display"):
        return str(ref["display"]).strip()
    return ""


def _obs_line(r: dict) -> str:
    name = _text(r.get("code"))
    if not name:
        return ""
    vq = r.get("valueQuantity")
    if isinstance(vq, dict) and vq.get("value") is not None:
        return f"{name} {vq['value']}{vq.get('unit', '')}".strip()
    v = _text(r.get("valueCodeableConcept")) or str(r.get("valueString") or "").strip()
    return f"{name} {v}".strip() if v else name


def _age(birth_date: str) -> int | None:
    """FHIR birthDate(YYYY[-MM[-DD]]) → 만 나이. 파싱 불가면 None."""
    parts = birth_date.split("-")
    try:
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 1
        d = int(parts[2]) if len(parts) > 2 else 1
        b = date(y, m, d)
    except (ValueError, IndexError):
        return None
    t = date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def build(bundle: dict[str, list[dict]]) -> PatientContext:
    """`FhirClient.patient_context()` 결과 → 프롬프트 맥락.

    ⚠️ **환자 인적사항을 넣지 않는다.** Patient 리소스는 받더라도 이름·식별번호를
    쓰지 않고, 임상 판단에 필요한 연령대·성별만(있으면) 쓴다.
    """
    ctx = PatientContext()

    # 성별·연령 — 임상 판단(예: 임신 관련 주제 배제)에 필요한 최소 인구학만.
    for p in (bundle.get("Patient") or [])[:1]:
        sex = {"male": "남성", "female": "여성"}.get(str((p or {}).get("gender") or ""))
        age = _age(str((p or {}).get("birthDate") or ""))
        demo = ", ".join(x for x in (sex, f"{age}세" if age is not None else None) if x)
        if demo:
            ctx.demographics = demo
            ctx.lines.append("환자: " + demo)

    conds = [_text(r.get("code")) for r in bundle.get("Condition") or []]
    conds = [c for c in conds if c][: _LIMITS["Condition"]]
    if conds:
        ctx.lines.append("진단: " + ", ".join(conds))

    obs = [_obs_line(r) for r in bundle.get("Observation") or []]
    obs = [o for o in obs if o][: _LIMITS["Observation"]]
    if obs:
        ctx.lines.append("검사: " + ", ".join(obs))

    meds_raw = bundle.get("MedicationRequest")
    if meds_raw is not None:
        ctx.medications_available = True
        meds = [_med_name(r) for r in meds_raw]
        meds = [m for m in meds if m][: _LIMITS["MedicationRequest"]]
        ctx.drugs = list(dict.fromkeys(meds))       # 순서 보존 중복 제거
        if meds:
            ctx.lines.append("현재 투약: " + ", ".join(ctx.drugs))

    alg_raw = bundle.get("AllergyIntolerance")
    if alg_raw is not None:
        ctx.allergies_available = True
        alg = [_text(r.get("code")) for r in alg_raw]
        alg = [a for a in alg if a][: _LIMITS["AllergyIntolerance"]]
        if alg:
            ctx.lines.append("알레르기: " + ", ".join(alg))

    reports = [_text(r.get("code")) for r in bundle.get("DiagnosticReport") or []]
    reports = [x for x in reports if x][: _LIMITS["DiagnosticReport"]]
    if reports:
        ctx.lines.append("판독: " + ", ".join(reports))

    procs = [_text(r.get("code")) for r in bundle.get("Procedure") or []]
    procs = [p for p in procs if p][: _LIMITS["Procedure"]]
    if procs:
        ctx.lines.append("시술·수술: " + ", ".join(procs))

    return ctx
