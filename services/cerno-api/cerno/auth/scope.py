"""접근범위 계산 — 어떤 컬렉션을 어떤 필터로 검색할지 **서버가** 정한다.

01_구현계획.md §3.2 불변 규칙 10:
클라이언트가 컬렉션·필터를 지정하는 경로를 두지 않는다. AI 서버로 나가는 모든 RAG 호출의
`filter` 는 이 모듈의 산출물로만 구성한다.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import Identity

PRIVATE = "cerno_priv"
GROUP = "cerno_grp"
ORG = "cerno_org"

_MAX_FILTER_KEYS = 10
"""AI 서버 `_build_where` 상한(routes/rag.py:29). 초과 시 $in 단일 키로 접는다."""


@dataclass(frozen=True)
class CollectionScope:
    collection: str
    where: dict | None
    visibility: str


def access_scopes(ident: Identity, groups: list[str]) -> list[CollectionScope]:
    """의료진이 검색할 수 있는 컬렉션과 필터. 항상 최대 3개 — 인원과 무관하다.

    - private: 본인 소유 문서만
    - group:   소속 그룹 문서만. 그룹이 없으면 이 컬렉션은 검색하지 않는다.
    - org:     전체 공개

    그룹 수가 많아도 `$in` 단일 키라 `_MAX_FILTER_KEYS` 를 넘지 않는다.
    """
    scopes = [
        CollectionScope(PRIVATE, {"owner": ident.clinician_id}, "private"),
    ]
    if groups:
        scopes.append(CollectionScope(GROUP, {"group": {"$in": list(groups)}}, "group"))
    scopes.append(CollectionScope(ORG, None, "org"))
    return scopes
