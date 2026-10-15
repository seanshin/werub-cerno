"""FastAPI 의존성 — 신원 검증과 공용 자원.

모든 라우트가 `require_identity` 를 거친다. 신원 없이 도달 가능한 경로는 `/health` 뿐이다.
"""
from __future__ import annotations

import functools
import os

import yaml
from fastapi import Depends, Header, HTTPException, Request

from ..auth import Identity, IdentityError, verify_identity
from ..clients.ai import AiClient
from ..library.store import DocumentStore

_CONFIG_DIR = os.environ.get("CERNO_CONFIG_DIR", "config")


def require_identity(request: Request) -> Identity:
    """`X-Cerno-*` 서명 헤더 → Identity. 위조·만료는 401.

    프록시(cerno-web)가 세션을 검증하고 서명해 붙인다. api 는 외부에 직접 노출되지
    않지만, 여기서도 독립 검증해 단일 실패점을 없앤다(불변 규칙 6).
    """
    try:
        return verify_identity(request.headers, os.environ.get("CERNO_API_KEY", ""))
    except IdentityError as e:
        raise HTTPException(status_code=401, detail=str(e)) from None


@functools.lru_cache(maxsize=1)
def _clinicians_config() -> dict:
    path = os.path.join(_CONFIG_DIR, "clinicians.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def clinician_ids() -> list[str]:
    """config 에 등록된 의료진 id 전체 — 기동 시 키맵 대조용 (17 기획 P0-4)."""
    cfg = _clinicians_config()
    return [str(c.get("id")) for c in cfg.get("clinicians") or [] if c.get("id")]


def groups_of(ident: Identity) -> list[str]:
    """의료진 소속 그룹.

    ⚠️ config 에 없는 의료진은 **그룹 없음**으로 동작한다(거부하지 않는다). 신원 출처는
    HIS 이고 이 파일은 프로파일일 뿐이므로, 미등록이 곧 차단이면 신규 의료진이 진입
    자체를 못 한다. 대신 그룹 서가에는 접근하지 못한다 — 안전한 쪽으로 기운다.
    """
    cfg = _clinicians_config()
    for c in cfg.get("clinicians") or []:
        if str(c.get("id")) == ident.clinician_id:
            return [str(g) for g in (c.get("groups") or [])]
    return []


@functools.lru_cache(maxsize=1)
def get_store() -> DocumentStore:
    return DocumentStore()


@functools.lru_cache(maxsize=1)
def get_ai() -> AiClient:
    return AiClient()


IdentityDep = Depends(require_identity)
