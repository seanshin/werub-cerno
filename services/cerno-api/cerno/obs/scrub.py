"""로그 PHI 스크러빙 — 식별자가 로그로 새는 것을 소스에서 막는다.

이식 원본: twin/twin/log_scrub.py (twin 아키텍처 불변식 감사의 **유일 BLOCKER** 였다)

Cerno 는 twin 보다 보호 대상이 하나 넓다. twin 은 환자 식별자만 다뤘지만 Cerno 에는
**의료진 식별자**가 있다 — `clinician_id` 는 곧 개인 서고의 소유 키이므로, 로그에
평문으로 남으면 "누가 무엇을 물었는지" 가 재구성된다.

접근 원칙: **access log 를 끄지 않는다.** 관찰성을 포기하는 대신 식별자만 치환한다.
로그를 지우면 사고 조사가 불가능해지고, 그건 다른 종류의 위험이다.
"""
from __future__ import annotations

import hashlib
import logging
import re

REDACT = "<redacted>"

# FHIR 참조 — Patient/{id}, Practitioner/{id}
_REF_RE = re.compile(r"\b(Patient|Practitioner|Encounter)/([^/?\s\"'\]}<>&,]+)")
# 경로 세그먼트 — /patient/{id}, /patients/{id}
_PATH_RE = re.compile(r"(/patients?/)([^/?\s]+)")
# UUID (FHIR 리소스 id)
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# 한국 주민등록번호 형태
_RRN_RE = re.compile(r"\b\d{6}\s*-\s*[1-4]\d{6}\b")
# 전화번호
_TEL_RE = re.compile(r"\b01[016-9][-\s]?\d{3,4}[-\s]?\d{4}\b")
# 신원 헤더가 로그에 통째로 실릴 경우
_HDR_RE = re.compile(r"(X-Cerno-User\s*[:=]\s*)([^\s,;]+)", re.IGNORECASE)
# API 키 — 사고 조사 중 헤더를 덤프하는 경우가 실제로 있다
_KEY_RE = re.compile(r"\b(sk-[A-Za-z0-9]{8})[A-Za-z0-9]+\b")


def scrub(text: str) -> str:
    """식별자 패턴을 마스킹. 멱등이다(이미 마스킹된 문자열에 다시 적용해도 안전)."""
    if not text:
        return text
    text = _RRN_RE.sub(REDACT, text)
    text = _TEL_RE.sub(REDACT, text)
    text = _REF_RE.sub(rf"\g<1>/{REDACT}", text)
    text = _PATH_RE.sub(rf"\g<1>{REDACT}", text)
    text = _UUID_RE.sub(REDACT, text)
    text = _HDR_RE.sub(rf"\g<1>{REDACT}", text)
    text = _KEY_RE.sub(r"\g<1>…", text)
    return text


def hash_id(value: str) -> str:
    """관측용 안정 해시. 같은 의료진은 같은 값이라 추이는 볼 수 있고 역산은 안 된다."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


class MessageScrubFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        try:
            msg = record.getMessage()
        except Exception:
            return True
        scrubbed = scrub(msg)
        if scrubbed != msg:
            record.msg = scrubbed
            record.args = ()
        return True


class AccessLogScrubFilter(logging.Filter):
    """uvicorn.access 용 — full path 인자만 치환(AccessFormatter 호환)."""

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        args = record.args
        if isinstance(args, tuple) and len(args) == 5 and isinstance(args[2], str):
            scrubbed = scrub(args[2])
            if scrubbed != args[2]:
                record.args = (args[0], args[1], scrubbed, args[3], args[4])
        return True


_installed = False


def install() -> None:
    """앱·uvicorn 로거에 스크러버 부착. 멱등."""
    global _installed
    if _installed:
        return
    logging.getLogger("uvicorn.access").addFilter(AccessLogScrubFilter())
    msg = MessageScrubFilter()
    for name in ("cerno", "uvicorn", "uvicorn.error"):
        logging.getLogger(name).addFilter(msg)
    for h in list(logging.getLogger().handlers):
        h.addFilter(msg)
    _installed = True
