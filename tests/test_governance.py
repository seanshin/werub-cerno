"""거버넌스 게이트 계약 테스트 (백서 §5 · 사양서 §5).

거버넌스는 경고가 아니라 **거부**다. 벡터DB 에 들어간 PHI·무출처 문서는 사후 제거가
어렵고 제거 확인은 더 어렵다.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.library.manifest import (
    GovernanceError, Manifest, Source, parse, validate, validate_or_raise,
)

ME = "12345"
MY_GROUPS = ["cardio-dept", "research-a"]


def m(**kw) -> Manifest:
    base = dict(
        doc_id="doc-1", title="제목", visibility="private",
        owner_clinician_id=ME, phi_reviewed=True,
    )
    base.update(kw)
    return Manifest(**base)


def check(mf: Manifest, uploader=ME, groups=None) -> list[str]:
    return validate(mf, uploader_id=uploader, uploader_groups=groups if groups is not None else MY_GROUPS)


# ── 전 계층 공통: 비식별화 ──────────────────────────────────

def test_phi_unreviewed_rejected():
    assert any("phi_reviewed" in x for x in check(m(phi_reviewed=False)))


def test_minimal_private_doc_passes():
    assert check(m()) == []


# ── 소유 정합성 — private 격리의 실질 ───────────────────────

def test_cannot_upload_under_someone_elses_name():
    """owner 필터가 곧 접근통제라, 타인 명의 적재를 막지 않으면 남의 서고에 밀어넣을 수 있다."""
    assert any("업로더와 다릅니다" in x for x in check(m(owner_clinician_id="99999")))


def test_uploader_identity_is_the_authority_not_the_manifest():
    """매니페스트끼리 비교하면 검증이 무의미하다 — 검증된 신원과 대조해야 한다."""
    mf = m(owner_clinician_id="99999")
    assert check(mf, uploader="99999") == []      # 본인이면 통과
    assert check(mf, uploader=ME) != []            # 남이면 거부


# ── 그룹 정합성 ─────────────────────────────────────────────

def test_group_doc_requires_groups():
    assert any("groups 가 최소 1개" in x for x in check(m(visibility="group")))


def test_cannot_target_group_you_dont_belong_to():
    v = check(m(visibility="group", groups=["secret-dept"]))
    assert any("소속되지 않은 그룹" in x for x in v)


def test_group_doc_with_own_group_passes():
    assert check(m(visibility="group", groups=["cardio-dept"])) == []


def test_private_doc_cannot_carry_groups():
    assert any("groups 를 지정할 수 없습니다" in x for x in check(m(groups=["cardio-dept"])))


# ── 공용 서고: 출처·저작권 ──────────────────────────────────

def test_org_doc_requires_provenance():
    v = check(m(visibility="org", license_approved=True))
    assert any("출처" in x for x in v)


def test_org_doc_requires_license_approval():
    v = check(m(visibility="org", source=Source(doi="10.1000/x")))
    assert any("license_approved" in x for x in v)


def test_org_doc_with_provenance_and_license_passes():
    assert check(m(visibility="org", license_approved=True,
                   source=Source(doi="10.1000/x", publication="NEJM", year=2025))) == []


def test_publication_alone_counts_as_provenance():
    assert Source(publication="대한당뇨병학회 진료지침").has_provenance


# ── 위반은 전량 보고한다 ────────────────────────────────────

def test_all_violations_reported_at_once():
    """하나씩 고치게 하지 않는다."""
    v = check(m(doc_id="", title="", visibility="org", phi_reviewed=False))
    assert len(v) >= 4


def test_validate_or_raise_carries_violations():
    with pytest.raises(GovernanceError) as e:
        validate_or_raise(m(phi_reviewed=False), uploader_id=ME, uploader_groups=[])
    assert e.value.violations


# ── 도메인 태그 표기 ────────────────────────────────────────

def test_unknown_modality_rejected():
    assert any("modality" in x for x in check(m(modalities=["mri", "hologram"])))


def test_bad_icd_code_rejected():
    assert any("ICD-10" in x for x in check(m(icd_codes=["I21", "당뇨"])))


def test_valid_domain_tags_pass():
    assert check(m(modalities=["ct", "mri"], icd_codes=["I21", "E11.9"], specialty="cardiology")) == []


# ── 메타데이터: 검색 필터와 키가 일치해야 한다 ──────────────

def test_metadata_owner_key_matches_scope_filter():
    """auth/scope.py 의 filter_template 과 키가 어긋나면 격리가 조용히 풀린다."""
    from cerno.auth import Identity
    from cerno.auth.scope import access_scopes

    meta = m().metadata()
    priv = [s for s in access_scopes(Identity(ME, "clinician"), []) if s.collection == "cerno_priv"][0]
    assert set(priv.where.keys()) == {"owner"}
    assert meta["owner"] == ME


def test_metadata_group_key_matches_scope_filter():
    from cerno.auth import Identity
    from cerno.auth.scope import access_scopes

    meta = m(visibility="group", groups=["cardio-dept"]).metadata()
    grp = [s for s in access_scopes(Identity(ME, "clinician"), MY_GROUPS) if s.collection == "cerno_grp"][0]
    assert set(grp.where.keys()) == {"group"}
    assert meta["group"] == "cardio-dept"


def test_metadata_values_are_scalars():
    """ChromaDB 메타데이터는 스칼라만 허용한다."""
    meta = m(modalities=["ct", "mri"], icd_codes=["I21"]).metadata()
    assert all(isinstance(v, (str, int, float, bool)) for v in meta.values())


# ── 인용 라벨: 개인 자료는 내용·출처를 노출하지 않는다 ──────

def test_private_citation_is_opaque():
    label = m(title="내 미발표 연구 노트").citation_label()
    assert label == "개인 자료 (비공개)"
    assert "미발표" not in label


def test_org_citation_shows_source():
    label = m(visibility="org", source=Source(authors=["Kim", "Lee"], publication="NEJM",
                                              year=2025, doi="10.1000/x")).citation_label()
    assert "NEJM" in label and "10.1000/x" in label and "Kim 외" in label


def test_group_citation_names_group_not_content():
    label = m(visibility="group", groups=["cardio-dept"], title="부서 내부 프로토콜").citation_label()
    assert "cardio-dept" in label and "프로토콜" not in label


# ── 파싱 ────────────────────────────────────────────────────

def test_parse_yaml_shaped_dict():
    mf = parse({
        "doc_id": "d1", "title": "t", "visibility": "org",
        "owner_clinician_id": ME, "phi_reviewed": True, "license_approved": True,
        "source": {"authors": ["A"], "publication": "P", "year": "2025", "doi": "10.1/x"},
        "modalities": ["ct"], "icd_codes": ["I21"],
    })
    assert mf.source.year == 2025          # 문자열 연도도 흡수
    assert check(mf) == []


def test_parse_rejects_non_object_source():
    with pytest.raises(GovernanceError):
        parse({"doc_id": "d1", "source": "NEJM"})
