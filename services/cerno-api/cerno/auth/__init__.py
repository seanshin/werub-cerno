"""신원 헤더 검증과 접근범위 계산 — cerno-api 의 모든 요청이 여기를 통과한다.

01_구현계획.md §3.2 불변 규칙 4·7·10 을 이행한다.

핵심: `clinician_id` 는 **검증된 서명 헤더에서만** 나온다. 요청 본문·쿼리에서 읽지 않는다.
AI 서버 RAG 인증은 API 키(테넌트) 단위라 `filter:{owner:...}` 위조를 서버가 막지 못한다 —
격리 책임 전체가 Cerno 에 있다.
"""
from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass

MAX_SKEW_SECONDS = 5
"""exp 는 프록시가 now+60 으로 발급한다. 약간의 시계 오차만 허용하고 미래값은 거부한다."""


class IdentityError(Exception):
    """신원 헤더 검증 실패. 호출측은 401 로 응답한다."""


@dataclass(frozen=True)
class Identity:
    clinician_id: str
    """HIS Practitioner id. RAG 소유자 격리 키."""
    role: str

    @property
    def log_id(self) -> str:
        """로그·관측용 해시. 원문 id 를 로그에 남기지 않는다."""
        return hashlib.sha256(self.clinician_id.encode()).hexdigest()[:16]


def verify_identity(headers, api_key: str, *, now: float | None = None) -> Identity:
    """`X-Cerno-User/-Role/-Sig/-Exp` 검증 → Identity.

    프록시가 `HMAC(api_key, f"{uid}.{role}.{exp}")` 로 서명한다. 위조 헤더를 거부하기
    위한 것이며, exp 로 재전송 창을 60초로 제한한다.
    """
    uid = (headers.get("X-Cerno-User") or "").strip()
    role = (headers.get("X-Cerno-Role") or "").strip()
    sig = (headers.get("X-Cerno-Sig") or "").strip()
    exp_raw = (headers.get("X-Cerno-Exp") or "").strip()

    if not (uid and role and sig and exp_raw):
        raise IdentityError("identity headers missing")
    if not api_key:
        raise IdentityError("server misconfigured: CERNO_API_KEY unset")

    try:
        exp = int(exp_raw)
    except ValueError:
        raise IdentityError("bad exp") from None

    t = time.time() if now is None else now
    if exp < t:
        raise IdentityError("identity expired")
    if exp > t + 60 + MAX_SKEW_SECONDS:
        raise IdentityError("exp too far in future")

    expected = hmac.new(api_key.encode(), f"{uid}.{role}.{exp}".encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise IdentityError("bad signature")

    return Identity(clinician_id=uid, role=role)


def assert_no_clinician_override(payload: dict | None, ident: Identity) -> None:
    """불변 규칙 4 — 본문의 `clinician_id` 가 신원과 다르면 403.

    같더라도 무시하고 항상 `ident.clinician_id` 를 쓴다. 프록시가 1차로 막지만
    api 를 직접 부르는 경로(테스트·내부 호출)를 위해 여기서도 검사한다.
    """
    if not payload:
        return
    given = payload.get("clinician_id")
    if given is not None and given != ident.clinician_id:
        raise PermissionError("clinician_id 는 지정할 수 없습니다")
