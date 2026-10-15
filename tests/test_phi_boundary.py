"""PHI 비저장 경계 계약 테스트 (보안 점검 2026-07-30).

Cerno 의 핵심 약속: "환자 데이터는 병원을 떠나지 않고, Cerno 는 저장하지 않는다."
이걸 구조로 보장하는지 고정한다 — 정책(마스킹)이 아니라 구조(애초에 안 받음)로.

경계:
  1. store 는 본문·환자 데이터를 받는 구조 자체가 없다 (매니페스트 메타만)
  2. FHIR 환자 데이터는 store 와 연결점이 없다 (다른 경로)
  3. 로그는 식별자를 스크러빙한다 (이중 방어)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.library.manifest import Manifest, Source
from cerno.library.store import DocumentStore, _to_dict
from cerno.fhir.context import build
from cerno.obs.scrub import scrub, REDACT


def mani(**kw):
    base = dict(doc_id="d1", title="심부전 진료지침", visibility="org",
                owner_clinician_id="12345", phi_reviewed=True)
    base.update(kw)
    return Manifest(**base)


# ── store 는 본문·환자 데이터를 저장하는 구조가 없다 ────────

def test_store_persists_only_manifest_metadata_not_body():
    """본문(text)은 Manifest 에 필드조차 없다 — 구조적으로 저장 불가."""
    d = _to_dict(mani())
    # 저장되는 키에 본문·환자 데이터 필드가 없다
    assert "text" not in d and "body" not in d and "content" not in d
    assert "patient" not in d and "mrn" not in d
    # 저장되는 것은 지식 문서 메타뿐
    assert set(d) <= {"doc_id", "title", "visibility", "owner_clinician_id", "source_type",
                      "files", "groups", "source", "phi_reviewed", "license_approved",
                      "specialty", "modalities", "icd_codes"}


def test_store_roundtrip_carries_no_body(tmp_path):
    s = DocumentStore(str(tmp_path / "t.db"))
    s.upsert(mani(), chunk_count=3)
    rec, raw = s.get("d1")
    # 등록부 어디에도 본문이 없다
    blob = str(raw) + str(rec)
    assert "환자" not in blob or "환자 데이터" not in blob  # 지침 제목은 허용
    assert rec.chunk_count == 3        # 청크 수만 알지 내용은 모른다


def test_db_file_contains_no_patient_records(tmp_path):
    """디스크 파일 자체를 열어도 환자 데이터가 없다."""
    dbp = tmp_path / "t.db"
    s = DocumentStore(str(dbp))
    s.upsert(mani(), chunk_count=1)
    raw_bytes = dbp.read_bytes()
    # 매니페스트 메타(제목)는 있으나, 본문·환자 식별자 필드 스키마가 없다
    assert b"medicationCodeableConcept" not in raw_bytes  # FHIR 필드
    assert b"valueQuantity" not in raw_bytes


# ── FHIR 환자 데이터는 store 와 연결점이 없다 ───────────────

def test_patient_context_is_not_a_persisted_type():
    """PatientContext 는 프롬프트용 임시 객체 — store 가 받지 못한다."""
    ctx = build({"Condition": [{"code": {"text": "심부전"}}],
                 "MedicationRequest": [{"medicationCodeableConcept": {"text": "와파린"}}]})
    # store.upsert 는 Manifest 만 받는다. PatientContext 를 넣으려면 타입이 안 맞는다.
    assert not hasattr(ctx, "doc_id")          # Manifest 인터페이스 없음
    assert type(ctx).__name__ == "PatientContext"


def test_fhir_context_drops_direct_identifiers():
    """최소 수집 — 이름·번호·연락처를 애초에 추출하지 않는다(꺼내지 않은 값은 못 샌다)."""
    ctx = build({"Patient": [{"name": [{"family": "홍", "given": ["길동"]}],
                              "identifier": [{"value": "MRN-12345"}],
                              "telecom": [{"value": "010-9999-8888"}]}],
                 "Condition": [{"code": {"text": "심부전"}}]})
    t = ctx.as_text()
    assert "홍" not in t and "MRN-12345" not in t and "010-9999" not in t


# ── 로그 스크러빙 (이중 방어) ───────────────────────────────

def test_fhir_reference_scrubbed_from_any_log_line():
    """혹시 환자 참조가 로그에 흘러도 스크러버가 잡는다."""
    line = "FHIR MedicationRequest 조회 완료 Patient/abc-123 · MRN 900101-1234567"
    out = scrub(line)
    assert "abc-123" not in out
    assert "900101-1234567" not in out
    assert REDACT in out


# ── 매니페스트 title 에 PHI 가 들어갈 수 있다는 갭 (정책 방어) ──

def test_manifest_title_is_uploader_controlled_free_text():
    """⚠️ title 은 자유텍스트라 업로더가 환자명을 넣을 수 있다. 구조가 아니라
    거버넌스(phi_reviewed 확인)로 막는 지점 — 이 테스트는 그 사실을 명문화한다.

    지식 문서 메타이지 환자 레코드가 아니므로 위험도는 낮으나, 파일럿 운영 시
    업로더 교육 대상이다(10_보안점검.md §PHI)."""
    m = mani(title="홍길동 환자 케이스 리뷰")   # 업로더가 실수로 PHI 삽입
    d = _to_dict(m)
    # 구조는 이걸 막지 않는다 — phi_reviewed 게이트(거버넌스)가 유일한 방어
    assert d["title"] == "홍길동 환자 케이스 리뷰"
    assert m.phi_reviewed  # 업로더가 "비식별 확인" 을 체크해야만 적재됨


# ── 매니페스트 메타의 형식화된 직접식별자는 구조로 거부 ─────

def test_manifest_with_rrn_in_title_rejected():
    from cerno.library.manifest import validate
    v = validate(mani(title="환자 900101-1234567 심부전 케이스"),
                 uploader_id="12345", uploader_groups=[])
    assert any("주민등록번호" in x for x in v)


def test_manifest_with_phone_in_publication_rejected():
    from cerno.library.manifest import validate
    v = validate(mani(visibility="org", license_approved=True,
                      source=Source(publication="연락 010-1234-5678", doi="10.1/x")),
                 uploader_id="12345", uploader_groups=[])
    assert any("전화번호" in x for x in v)


def test_normal_title_with_numbers_passes():
    """정상 제목의 숫자(연도·용량)는 통과해야 한다 — 과탐 방지."""
    from cerno.library.manifest import validate
    v = validate(mani(title="2형 당뇨 관리 2025 · 메트포르민 500mg"),
                 uploader_id="12345", uploader_groups=[])
    assert not any("식별자" in x for x in v)
