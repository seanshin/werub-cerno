"""근거 검색 — 3컬렉션 병렬 조회 → RRF 병합 → 근거 게이트.

01_구현계획.md §3.1·§4 (5~7단계).

세 가지를 지킨다.
  1. **접근범위는 서버가 계산한다** — 호출자가 컬렉션·필터를 넘기지 않는다.
  2. **병합은 RRF** — 컬렉션마다 점수 분포가 다르므로 원점수 정렬은 틀린다.
  3. **게이트 미달이면 생성하지 않는다** — 빈 근거를 추측으로 메우지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass

from ..auth import Identity
from ..auth.scope import CollectionScope, access_scopes
from ..circuit import CircuitOpen
from ..clients.ai import AiBusy, AiClient, AiUnavailable, Hit

log = logging.getLogger("cerno.ask")

RRF_K = 60
"""AI 서버 hybrid_search 와 동일한 상수. 순위 기반이라 컬렉션 간 점수 스케일 차이에 둔감하다."""

DEFAULT_CONFIDENCE_MIN = float(os.environ.get("CERNO_CONFIDENCE_MIN", "0.45"))
"""근거 게이트 임계. **`mode=vector` 의 코사인 점수 기준이다.**

라이브 실측(2026-07-30 · Cerno 코퍼스):
  임상 질의   0.69 / 0.74 (1순위) · 0.38 / 0.44 (2순위·무관 문서)
  무의미 질의 **0건 반환**
→ 0.45 는 1순위 관련 문서를 통과시키고 2순위 무관 문서를 거른다. 기준선 유지.

⚠️ `mode=hybrid` 로 바꾸면 점수가 RRF 순위 값(~0.01)이 되어 **전건이 게이트에 걸려
Cerno 가 아무 답도 못 한다.** clients/ai.py 가 hybrid 에서 confidence 를 우선 읽는 이유다(RRF score 를 읽으면 전건 탈락)."""

DEFAULT_MIN_EVIDENCE = int(os.environ.get("CERNO_MIN_EVIDENCE", "1"))

OVERFETCH = 2
"""컬렉션당 요청 배수. dedup 으로 줄어든 슬롯을 보충한다(AI 서버 v2.124.17 과 동일 값).
⚠️ rate limit 은 **요청 수** 기준이라 오버페치는 호출 수를 늘리지 않는다(n_results 만 증가)."""


# Evidence 정의는 `personal` 에 하나만 둔다 — 두 벌로 두었다가 rank 유무가 갈려
# 프롬프트 조립이 깨질 뻔했다(계약 테스트가 잡음). 여기서는 재수출만 한다.
from ..personal import Evidence  # noqa: E402  (재수출)


@dataclass(frozen=True)
class SearchResult:
    evidence: list[Evidence]
    grounded: bool
    """False 면 생성하지 않고 '근거로 확인되지 않음' 으로 응답한다."""
    reason: str = ""
    degraded: bool = False
    """일부 컬렉션 조회가 실패했다. 근거가 있어도 불완전할 수 있음을 상위에 알린다."""


def _citation(hit: Hit, visibility: str) -> str:
    """개인·그룹 자료는 내용·출처를 노출하지 않는다(백서 §5 인용 규약)."""
    if visibility == "private":
        return "개인 자료 (비공개)"
    if visibility == "group":
        g = hit.metadata.get("group")
        return f"그룹 자료 ({g})" if g else "그룹 자료"
    return str(hit.metadata.get("citation") or "출처 미상")


def parent_doc_id(doc_id: str) -> str:
    """청크·그룹 전개 id → 부모 문서 id.

    형식: `{doc_id}[__grp_{group}]__chunk_{n}` (`library/chunk.py::chunk_id` 와 쌍)

    그룹 접미까지 벗기는 이유: 다중 그룹 문서는 그룹마다 별도 색인되므로, 벗기지 않으면
    **같은 문서가 그룹 수만큼 중복 인용**된다.
    """
    return doc_id.split("__grp_", 1)[0].split("__chunk_", 1)[0]


def rrf_merge(
    per_collection: list[tuple[CollectionScope, list[Hit]]],
    *,
    weights: dict[str, float] | None = None,
    k: int = RRF_K,
) -> list[tuple[Hit, str, float]]:
    """Reciprocal Rank Fusion + **문서 단위 중복 제거**. 반환은 (hit, visibility, confidence).

    컬렉션마다 유사도 분포가 달라 원점수로 정렬하면 한 컬렉션이 상위를 독식한다.
    순위 기반 융합이 그 왜곡을 없앤다. `weights` 로 개인 서고 우선을 준다(Phase 4-A2).

    ⚠️ **중복 제거가 필수다.** 한 문서가 여러 청크로 색인되므로 dedup 없이는 상위 슬롯을
    같은 문서가 잠식해 다른 근거가 밀려난다 — AI 서버가 실제로 겪은 문제로, 중복이
    top-8 의 15% 를 먹어 **권위 위계가 조용히 훼손**됐다(v2.124.17).

    점수는 **합이 아니라 max** 를 쓴다. 합산하면 청크가 많은 문서가 내용과 무관하게
    상위로 올라간다(청크 수 편향).
    """
    weights = weights or {}
    fused: dict[str, float] = {}
    holder: dict[str, tuple[Hit, str, int]] = {}   # key → (hit, visibility, best_rank)

    for scope, hits in per_collection:
        w = weights.get(scope.visibility, 1.0)
        for rank, h in enumerate(hits, start=1):
            key = parent_doc_id(h.doc_id)
            score = w / (k + rank)
            if score > fused.get(key, -1.0):
                fused[key] = score
                holder[key] = (h, scope.visibility, rank)   # 대표 = 가장 높은 순위의 청크

    ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)
    return [(holder[key][0], holder[key][1], holder[key][0].score) for key, _ in ordered]


async def search_evidence(
    query: str,
    *,
    ident: Identity,
    groups: list[str],
    ai: AiClient,
    n_per_collection: int = 5,
    top_k: int = 6,
    confidence_min: float = DEFAULT_CONFIDENCE_MIN,
    min_evidence: int = DEFAULT_MIN_EVIDENCE,
    weights: dict[str, float] | None = None,
) -> SearchResult:
    scopes = access_scopes(ident, groups)   # 서버가 계산 — 호출자 지정 경로 없음

    # 오버페치 후 dedup → 절단. dedup 만 하고 보충하지 않으면 슬롯이 줄어 그 자리를
    # 다른 컬렉션이 메우고, 결과적으로 권위 위계가 훼손된다(AI 서버 v2.124.17 동일 교훈).
    fetch_n = n_per_collection * OVERFETCH

    async def one(s: CollectionScope) -> tuple[CollectionScope, list[Hit], bool]:
        try:
            hits = await ai.search(
                query, clinician_id=ident.clinician_id,
                collection=s.collection, where=s.where, n_results=fetch_n,
            )
            return s, hits, True
        except AiBusy:
            raise                                   # 혼잡은 전체에 해당 — 올린다
        except (AiUnavailable, CircuitOpen) as e:
            # 한 컬렉션 실패가 나머지를 죽이지 않는다. 단 degraded 로 표시한다.
            log.info("컬렉션 %s 검색 실패: %s", s.collection, e)
            return s, [], False

    results = await asyncio.gather(*(one(s) for s in scopes))
    degraded = any(not ok for _, _, ok in results)

    merged = rrf_merge([(s, h) for s, h, _ in results], weights=weights)

    # 근거 게이트 — 임계 미달은 버린다. LLM 이 노이즈를 읽지 않게 **컨텍스트 구성 전** 적용한다.
    kept = [(h, v, c) for h, v, c in merged if c >= confidence_min][:top_k]

    if len(kept) < min_evidence:
        return SearchResult(
            evidence=[], grounded=False, degraded=degraded,
            reason=(
                "근거 조회가 일부 실패했습니다" if degraded and not merged
                else f"임계({confidence_min}) 이상 근거 {min_evidence}건 미만"
            ),
        )

    evidence = [
        Evidence(doc_id=h.doc_id, text=h.text, visibility=v,
                 confidence=c, citation=_citation(h, v), rank=i)
        for i, (h, v, c) in enumerate(kept, start=1)
    ]
    return SearchResult(evidence=evidence, grounded=True, degraded=degraded)
