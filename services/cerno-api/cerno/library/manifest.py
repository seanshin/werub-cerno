"""문서 매니페스트 스키마와 거버넌스 검증 (백서 §5 · 사양서 §5).

거버넌스는 **적재 게이트**다. 하나라도 위반하면 적재하지 않는다 — 경고하고 넣는 것이
아니라 거부한다. 벡터DB에 한 번 들어간 PHI·무출처 문서는 사후에 지우기 어렵고,
지웠는지 확인하기는 더 어렵다.

계층별 요건:
  private  본인 소유일 것
  group    업로더가 속한 그룹만 지정할 것
  org      출처(doi|url|publication) + license_approved — 기관 전체가 인용하므로
           출처 없는 문서가 들어가면 답변의 근거 표기가 거짓이 된다
전 계층 공통: phi_reviewed(비식별화 확인)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

Visibility = Literal["private", "group", "org"]

VISIBILITIES: tuple[str, ...] = ("private", "group", "org")

# 도메인 태그(백서 §6) — 검색 필터에 쓰이므로 표기를 고정한다
MODALITIES: frozenset[str] = frozenset(
    "text xray ct mri us pet pathology endoscopy fundus ecg lab".split()
)
_ICD_RE = re.compile(r"^[A-Z]\d{2}(\.\d{1,2})?$")   # KCD/ICD-10 (예: I21, E11.9)
_DOC_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

# 매니페스트 자유텍스트(title·출처)에서 거부할 **형식화된 직접식별자**.
# 이름은 판별 불가라 phi_reviewed 게이트에 맡기고, 확실한 형식만 구조로 막는다.
_RRN_RE = re.compile(r"\b\d{6}\s*-\s*[1-4]\d{6}\b")           # 주민등록번호
_TEL_RE = re.compile(r"\b01[016-9][-\s]?\d{3,4}[-\s]?\d{4}\b")  # 휴대폰


def _has_direct_identifier(*texts: str) -> str | None:
    """형식화된 직접식별자가 있으면 종류명, 없으면 None."""
    blob = " ".join(t for t in texts if t)
    if _RRN_RE.search(blob):
        return "주민등록번호 형식"
    if _TEL_RE.search(blob):
        return "전화번호 형식"
    return None


class GovernanceError(Exception):
    """적재 거부. `violations` 에 위반 사유 전량을 담는다(하나씩 고치게 하지 않는다)."""

    def __init__(self, violations: list[str]):
        super().__init__("; ".join(violations))
        self.violations = violations


@dataclass(frozen=True)
class Source:
    authors: list[str] = field(default_factory=list)
    publication: str = ""
    year: int | None = None
    doi: str = ""
    url: str = ""
    license: str = ""

    @property
    def has_provenance(self) -> bool:
        return bool(self.doi or self.url or self.publication)

    def citation(self) -> str:
        """공용 서고 인용 문자열. 개인 자료는 이 함수를 쓰지 않는다(§인용 규약)."""
        parts: list[str] = []
        if self.authors:
            parts.append(self.authors[0] + (" 외" if len(self.authors) > 1 else ""))
        if self.publication:
            parts.append(self.publication)
        if self.year:
            parts.append(str(self.year))
        head = ", ".join(parts)
        if self.doi:
            return f"{head} (DOI {self.doi})" if head else f"DOI {self.doi}"
        if self.url and not head:
            return self.url
        return head or "출처 미상"


@dataclass(frozen=True)
class Manifest:
    doc_id: str
    title: str
    visibility: Visibility
    owner_clinician_id: str
    source_type: str = ""
    files: list[str] = field(default_factory=list)
    groups: list[str] = field(default_factory=list)
    source: Source = field(default_factory=Source)
    phi_reviewed: bool = False
    license_approved: bool = False
    specialty: str = ""
    modalities: list[str] = field(default_factory=list)
    icd_codes: list[str] = field(default_factory=list)

    # ── 적재 메타 ────────────────────────────────────────────

    def metadata(self) -> dict[str, Any]:
        """AI 서버 `/api/rag/index` 의 metadata. **검색 필터가 여기에 의존한다.**

        `owner`·`group` 키 이름은 `auth/scope.py` 의 filter_template 과 반드시 일치해야
        한다 — 어긋나면 격리가 조용히 풀린다(필터가 아무것도 매칭하지 않거나 전부 매칭).

        ChromaDB 메타데이터는 스칼라만 허용하므로 리스트는 구분자 문자열로 편다.
        """
        m: dict[str, Any] = {
            "doc_id": self.doc_id,
            "visibility": self.visibility,
            "owner": self.owner_clinician_id,
            "title": self.title,
        }
        if self.groups:
            # 단일 그룹만 필터 대상. 다중 그룹 문서는 ingest 에서 그룹당 1건으로 전개한다.
            m["group"] = self.groups[0]
        if self.specialty:
            m["specialty"] = self.specialty
        if self.modalities:
            m["modalities"] = "|".join(sorted(self.modalities))
        if self.icd_codes:
            m["icd_codes"] = "|".join(sorted(self.icd_codes))
        if self.visibility == "org":
            m["citation"] = self.source.citation()
            if self.source.license:
                m["license"] = self.source.license
        return m

    def citation_label(self) -> str:
        """답변에 붙일 인용 라벨. 개인 자료는 내용·출처를 노출하지 않는다."""
        if self.visibility == "private":
            return "개인 자료 (비공개)"
        if self.visibility == "group":
            return f"그룹 자료 ({self.groups[0]})" if self.groups else "그룹 자료"
        return self.source.citation()


def parse(raw: dict[str, Any]) -> Manifest:
    """dict(YAML 로드 결과) → Manifest. 타입 오류는 여기서 GovernanceError 로 바꾼다."""
    v = []
    src_raw = raw.get("source") or {}
    if not isinstance(src_raw, dict):
        v.append("source 는 객체여야 합니다")
        src_raw = {}
    year = src_raw.get("year")
    if year is not None and not isinstance(year, int):
        try:
            year = int(year)
        except (TypeError, ValueError):
            v.append("source.year 는 정수여야 합니다")
            year = None
    if v:
        raise GovernanceError(v)

    def as_list(key: str, container: dict[str, Any]) -> list[str]:
        val = container.get(key) or []
        return [str(x) for x in val] if isinstance(val, (list, tuple)) else [str(val)]

    return Manifest(
        doc_id=str(raw.get("doc_id", "")),
        title=str(raw.get("title", "")),
        visibility=raw.get("visibility", ""),          # 검증은 validate 에서
        owner_clinician_id=str(raw.get("owner_clinician_id", "")),
        source_type=str(raw.get("source_type", "")),
        files=as_list("files", raw),
        groups=as_list("groups", raw),
        source=Source(
            authors=as_list("authors", src_raw),
            publication=str(src_raw.get("publication", "")),
            year=year,
            doi=str(src_raw.get("doi", "")),
            url=str(src_raw.get("url", "")),
            license=str(src_raw.get("license", "")),
        ),
        phi_reviewed=bool(raw.get("phi_reviewed", False)),
        license_approved=bool(raw.get("license_approved", False)),
        specialty=str(raw.get("specialty", "")),
        modalities=as_list("modalities", raw),
        icd_codes=as_list("icd_codes", raw),
    )


def validate(
    m: Manifest,
    *,
    uploader_id: str,
    uploader_groups: list[str],
) -> list[str]:
    """위반 사유 전량을 돌려준다. 빈 리스트면 적재 가능.

    ★ `uploader_id` 는 **검증된 신원**이어야 한다(auth 에서 온 clinician_id).
      매니페스트의 `owner_clinician_id` 를 신뢰해 자기 자신과 비교하면 검증이 무의미하다.
    """
    v: list[str] = []

    if not _DOC_ID_RE.match(m.doc_id or ""):
        v.append("doc_id 가 없거나 허용 문자(영숫자 . _ -)를 벗어납니다")
    if not m.title.strip():
        v.append("title 이 비었습니다")
    if m.visibility not in VISIBILITIES:
        v.append(f"visibility 는 {'|'.join(VISIBILITIES)} 중 하나여야 합니다")

    # ── 전 계층 공통: 비식별화 ──
    if not m.phi_reviewed:
        v.append("phi_reviewed=true 가 아닙니다 (비식별화 확인 필요)")
    # 매니페스트 메타는 저장·인용에 노출되므로 형식화된 직접식별자를 구조로 거부한다.
    # (본문 PHI 는 phi_reviewed 게이트, 이름은 판별 불가 — 여기선 확실한 형식만)
    ident = _has_direct_identifier(m.title, m.source.publication, " ".join(m.source.authors))
    if ident:
        v.append(f"메타데이터(제목·출처)에 {ident}가 있습니다 — 직접식별자 제거 필요")

    # ── 소유·그룹 정합성 ──
    if not m.owner_clinician_id:
        v.append("owner_clinician_id 가 없습니다")
    elif m.owner_clinician_id != uploader_id:
        # 타인 명의 적재 차단 — private 컬렉션의 owner 필터가 곧 접근통제이므로
        # 여기서 막지 않으면 남의 서고에 문서를 밀어 넣을 수 있다.
        v.append("owner_clinician_id 가 업로더와 다릅니다")

    if m.visibility == "group":
        if not m.groups:
            v.append("group 문서는 groups 가 최소 1개 필요합니다")
        else:
            outside = [g for g in m.groups if g not in uploader_groups]
            if outside:
                v.append(f"소속되지 않은 그룹을 지정했습니다: {', '.join(outside)}")
    elif m.groups:
        v.append(f"visibility={m.visibility} 문서에는 groups 를 지정할 수 없습니다")

    # ── 공용 서고: 출처·저작권 ──
    if m.visibility == "org":
        if not m.source.has_provenance:
            v.append("org 문서는 출처(doi 또는 url 또는 publication)가 필요합니다")
        if not m.license_approved:
            v.append("org 문서는 license_approved=true 가 필요합니다 (저작권 검토)")

    # ── 도메인 태그 표기 ──
    bad_mod = [x for x in m.modalities if x not in MODALITIES]
    if bad_mod:
        v.append(f"알 수 없는 modality: {', '.join(bad_mod)}")
    bad_icd = [x for x in m.icd_codes if not _ICD_RE.match(x)]
    if bad_icd:
        v.append(f"KCD/ICD-10 형식이 아닌 코드: {', '.join(bad_icd)}")

    return v


def validate_or_raise(m: Manifest, *, uploader_id: str, uploader_groups: list[str]) -> None:
    v = validate(m, uploader_id=uploader_id, uploader_groups=uploader_groups)
    if v:
        raise GovernanceError(v)
