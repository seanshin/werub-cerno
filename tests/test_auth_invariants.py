"""불변 규칙 계약 테스트 (01_구현계획.md §3.2).

이 테스트가 깨지면 개인 서고 격리가 무너진 것이다. 완화하지 말 것.
"""
import hashlib
import hmac
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.auth import Identity, IdentityError, assert_no_clinician_override, verify_identity
from cerno.auth.scope import ORG, PRIVATE, access_scopes

KEY = "test-api-key"


def headers(uid="12345", role="clinician", exp=None, key=KEY, sig=None):
    exp = exp if exp is not None else int(time.time()) + 60
    if sig is None:
        sig = hmac.new(key.encode(), f"{uid}.{role}.{exp}".encode(), hashlib.sha256).hexdigest()
    return {"X-Cerno-User": uid, "X-Cerno-Role": role, "X-Cerno-Sig": sig, "X-Cerno-Exp": str(exp)}


# ── 규칙 7: 신원 헤더 서명 ──────────────────────────────────────

def test_valid_identity_accepted():
    ident = verify_identity(headers(), KEY)
    assert ident.clinician_id == "12345"
    assert ident.role == "clinician"


def test_forged_signature_rejected():
    with pytest.raises(IdentityError):
        verify_identity(headers(sig="deadbeef"), KEY)


def test_signature_from_wrong_key_rejected():
    """다른 키로 서명한 헤더 = 위조."""
    with pytest.raises(IdentityError):
        verify_identity(headers(key="attacker-key"), KEY)


def test_tampered_uid_rejected():
    """서명은 유효하나 uid 만 바꿔치기 — 타 의료진 사칭 시도."""
    h = headers(uid="12345")
    h["X-Cerno-User"] = "99999"
    with pytest.raises(IdentityError):
        verify_identity(h, KEY)


def test_role_escalation_rejected():
    h = headers(role="clinician")
    h["X-Cerno-Role"] = "admin"
    with pytest.raises(IdentityError):
        verify_identity(h, KEY)


def test_expired_identity_rejected():
    with pytest.raises(IdentityError):
        verify_identity(headers(exp=int(time.time()) - 1), KEY)


def test_far_future_exp_rejected():
    """재전송 창을 60초로 제한 — 장기 유효 헤더를 만들 수 없다."""
    with pytest.raises(IdentityError):
        verify_identity(headers(exp=int(time.time()) + 86400), KEY)


def test_missing_headers_rejected():
    with pytest.raises(IdentityError):
        verify_identity({}, KEY)


def test_unset_server_key_rejected():
    """서버 키 미설정 시 통과시키지 않는다(fail-closed)."""
    with pytest.raises(IdentityError):
        verify_identity(headers(), "")


# ── 규칙 4: clinician_id 는 입력값이 아니다 ────────────────────

def test_body_clinician_id_mismatch_forbidden():
    ident = verify_identity(headers(uid="12345"), KEY)
    with pytest.raises(PermissionError):
        assert_no_clinician_override({"clinician_id": "99999", "query": "…"}, ident)


def test_body_without_clinician_id_ok():
    ident = verify_identity(headers(), KEY)
    assert_no_clinician_override({"query": "…"}, ident)


# ── 규칙 10: 접근범위는 서버가 계산한다 ────────────────────────

def test_private_scope_bound_to_identity():
    ident = Identity(clinician_id="12345", role="clinician")
    scopes = access_scopes(ident, groups=[])
    priv = [s for s in scopes if s.collection == PRIVATE]
    assert len(priv) == 1
    assert priv[0].where == {"owner": "12345"}


def test_no_group_collection_when_no_groups():
    """소속 그룹이 없으면 그룹 서가를 아예 검색하지 않는다."""
    scopes = access_scopes(Identity("12345", "clinician"), groups=[])
    assert {s.collection for s in scopes} == {PRIVATE, ORG}


def test_scope_count_independent_of_clinician_count():
    """컬렉션 수는 항상 최대 3 — 의료진이 늘어도 검색 호출이 늘지 않는다."""
    many_groups = [f"g{i}" for i in range(50)]
    scopes = access_scopes(Identity("12345", "clinician"), groups=many_groups)
    assert len(scopes) == 3


def test_group_filter_uses_single_in_key():
    """그룹이 많아도 $in 단일 키 — AI 서버 filter 키 상한 10 을 넘지 않는다."""
    many_groups = [f"g{i}" for i in range(50)]
    grp = [s for s in access_scopes(Identity("12345", "clinician"), many_groups) if s.collection == "cerno_grp"][0]
    assert list(grp.where.keys()) == ["group"]
    assert grp.where["group"]["$in"] == many_groups


def test_org_scope_has_no_owner_filter():
    org = [s for s in access_scopes(Identity("12345", "clinician"), []) if s.collection == ORG][0]
    assert org.where is None


# ── 관측: 원문 id 를 로그에 남기지 않는다 ──────────────────────

def test_log_id_is_hashed():
    ident = Identity("12345", "clinician")
    assert "12345" not in ident.log_id
    assert len(ident.log_id) == 16
