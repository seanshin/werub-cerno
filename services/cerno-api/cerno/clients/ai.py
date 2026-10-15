"""AI 서버(WeRU.B) 클라이언트 — RAG·생성·워밍의 단일 출구.

★ **의료진별 API 키** (AI 서버 회신 A-1)
AI 서버 공정 큐(`ChatFairQueue`)의 버킷 키가 **API 키의 sha256** 이다. 단일 키로 호출하면
`max_per_user=1` 에 걸려 의료진이 몇 명이든 동시 1건이 되고, 두 번째 질의부터 최대 60초
대기 후 429 다. 따라서 요청의 의료진에 맞는 키를 골라 쓴다.

부수 효과로 rate limit 120/min 도 의료진별이 되어, 3컬렉션 검색(질의당 3회) 기준
**의료진당 분당 40질의**가 된다.

⚠️ `max_global=3` 은 여전히 천장이다. 의료진별 키를 써도 4번째 동시 요청은 대기한다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

import httpx

from ..circuit import CircuitOpen, breaker

log = logging.getLogger("cerno.ai")

_TIMEOUT = float(os.environ.get("CERNO_AI_TIMEOUT", "120"))
_MAX_RETRY = 2


class AiUnavailable(RuntimeError):
    """AI 서버 장애·서킷 열림. 상위는 근거 없이 답하지 말고 안전 응답으로 degrade 한다."""


class AiBusy(RuntimeError):
    """공정 큐 대기 초과(429). 이 발생률이 GPU 증설 판단의 1차 지표다."""

    def __init__(self, retry_after: float = 10.0):
        super().__init__(f"AI 서버 혼잡, {retry_after:.0f}초 후 재시도")
        self.retry_after = retry_after


def load_key_map(raw: str | None = None) -> dict[str, str]:
    """CERNO_AI_KEYS 파싱 — **실패는 예외다** (17 기획 P0-4).

    과거에는 파싱 실패 시 로그 1줄 내고 빈 맵으로 진행했다. 그 결과 D-0(2026-08-04)
    에 비JSON 값이 들어가자 전 의료진이 공유 키로 조용히 강하했고, 빈 맵에서는
    "전용 키 없음" 경고마저 침묵해 게이트 3(전용 키 분리)이 무증상으로 무효였다.
    잘못된 설정으로는 뜨지 않는 쪽이 옳다(`${VAR:?}` 와 같은 철학).
    """
    if raw is None:
        raw = os.environ.get("CERNO_AI_KEYS", "")
    if not raw:
        return {}
    try:
        m = json.loads(raw)
    except ValueError as e:
        raise RuntimeError(
            'CERNO_AI_KEYS 가 JSON 이 아니다 — {"<clinician_id>":"sk-..."} 형식이어야 한다') from e
    if (not isinstance(m, dict)
            or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in m.items())):
        raise RuntimeError("CERNO_AI_KEYS 는 문자열→문자열 맵이어야 한다")
    return m


class KeyResolver:
    """clinician_id → API 키.

    운영에서는 시크릿 매니저를 붙인다. 현재는 환경변수 JSON 맵을 읽는다
    (`CERNO_AI_KEYS='{"12345":"sk-...","67890":"sk-..."}'`).
    설정 파일·저장소에 키를 두지 않는다.
    """

    def __init__(self, mapping: dict[str, str] | None = None, fallback: str | None = None):
        self._map = load_key_map() if mapping is None else mapping
        self._fallback = fallback if fallback is not None else os.environ.get("CERNO_AI_KEY", "")

    def key_for(self, clinician_id: str) -> str:
        k = self._map.get(clinician_id) or self._fallback
        if not k:
            raise AiUnavailable("AI 서버 API 키 미설정")
        if not self._map.get(clinician_id) and self._map:
            # 공유 키로 떨어지면 그 의료진은 다른 사람과 동시성 버킷을 공유한다.
            # 조용히 느려지는 원인이 되므로 남긴다.
            log.warning("clinician 전용 키 없음 → 공유 키 사용(동시성 버킷 공유)")
        return k


@dataclass(frozen=True)
class Hit:
    doc_id: str
    text: str
    score: float
    metadata: dict[str, Any]


class AiClient:
    def __init__(
        self,
        *,
        base_url: str | None = None,
        keys: KeyResolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base = (base_url or os.environ.get("CERNO_AI_BASE") or "").rstrip("/")
        if not self._base:
            raise RuntimeError("CERNO_AI_BASE is required")
        self._keys = keys or KeyResolver()
        self._transport = transport

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport)

    def _headers(self, clinician_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._keys.key_for(clinician_id)}",
            "Content-Type": "application/json",
            # PHI 비저장 — 전역 medical_phi_store 차단이 있어도 호출 측에서 명시한다
            "X-No-Store": "1",
        }

    async def _post(self, path: str, payload: dict, *, clinician_id: str) -> Any:
        return await self._request("POST", path, clinician_id=clinician_id, payload=payload)

    async def _request(
        self, method: str, path: str, *, clinician_id: str,
        payload: dict | None = None, params: dict | None = None,
    ) -> Any:
        br = breaker("ai")
        if not br.allow():
            raise CircuitOpen("AI 서버 서킷 열림")

        headers = self._headers(clinician_id)
        try:
            async with self._client() as c:
                for attempt in range(_MAX_RETRY):
                    r = await c.request(method, f"{self._base}{path}",
                                        json=payload, params=params, headers=headers)
                    if r.status_code != 429:
                        break
                    wait = _retry_after(r)
                    if attempt + 1 >= _MAX_RETRY:
                        br.record_success()   # 혼잡은 장애가 아니다 — 서킷을 열지 않는다
                        raise AiBusy(wait)
                    log.info("AI 429 → %.1fs 후 재시도(%d/%d)", wait, attempt + 1, _MAX_RETRY)
                    await asyncio.sleep(wait)

                if r.status_code >= 500:
                    br.record_failure()
                    raise AiUnavailable(f"AI {r.status_code} ({path})")
                if r.status_code >= 400:
                    # 4xx 는 우리 요청 문제 — 서킷 대상 아님
                    br.record_success()
                    raise AiUnavailable(f"AI {r.status_code} ({path})")
                br.record_success()
                _record_queue_headers(r)
                return r.json()
        except (AiBusy, AiUnavailable, CircuitOpen):
            raise
        except httpx.HTTPError:
            br.record_failure()
            raise AiUnavailable(f"AI 통신 실패 ({path})") from None

    # ── RAG ─────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        *,
        clinician_id: str,
        collection: str,
        where: dict | None,
        n_results: int = 5,
        mode: str = "vector",
        rerank: bool = True,
    ) -> list[Hit]:
        """유사도는 `_confidence()` 로 꺼낸다 — **모드마다 필드가 다르다.**

        이력(2026-07-30): `hybrid` 가 메타데이터 필터를 우회해(owner=BBB 유출) 한때
        `ValueError` 로 금지했다. AI 서버 v2.124.65 에서 봉합됐고 **원래 재현 절차로
        재검증해 해제**했다 — filter owner=AAA→AAA만, owner=NOBODY→0건.

        기본값은 `vector` 를 유지한다. hybrid 도 안전해졌지만, 임계 0.45 와 N=10 기준선을
        vector 로 잡았으므로 기본값을 바꾸면 측정된 시스템이 조용히 달라진다.
        전환은 실 코퍼스에서 재현율 이득을 재고 기준선을 다시 잡은 뒤에 한다.
        """
        payload: dict[str, Any] = {
            "query": query, "collection": collection,
            "n_results": n_results, "mode": mode, "rerank": rerank,
        }
        if where:
            payload["filter"] = where
        data = await self._post("/api/rag/search", payload, clinician_id=clinician_id)
        out: list[Hit] = []
        for r in data.get("results") or []:
            c = _confidence(r)
            if c is None:
                # FTS-only 히트는 confidence 가 null 이다 — 유사도 판정이 불가능하다.
                # 게이트를 통과시킬 근거가 없으므로 버린다(AI 서버 v2.124.65 안내).
                log.debug("confidence 없는 히트 제외: %s", r.get("id"))
                continue
            out.append(Hit(
                doc_id=str(r.get("id", "")),
                text=str(r.get("text", "")),
                score=c,
                metadata=r.get("metadata") or {},
            ))
        return out

    async def index_batch(
        self, docs: list[dict], *, clinician_id: str, collection: str
    ) -> dict:
        if len(docs) > 500:
            raise ValueError("배치 상한 500건")
        return await self._post(
            "/api/rag/index", {"documents": docs, "collection": collection},
            clinician_id=clinician_id,
        )

    async def delete_doc(self, doc_id: str, *, clinician_id: str, collection: str) -> dict:
        """멱등 — 없는 id 도 ok. ⚠️ DELETE 도 키당 120/min 에 포함된다."""
        return await self._request(
            "DELETE", f"/api/rag/document/{doc_id}",
            clinician_id=clinician_id, params={"collection": collection},
        )

    # ── 생성 ────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        *,
        clinician_id: str,
        model: str | None = None,
        temperature: float = 0.2,
        seed: int | None = 42,
        num_predict: int = 800,
    ) -> str:
        payload: dict[str, Any] = {
            "model": model or os.environ.get("CERNO_AI_MODEL", "qwen2.5:14b"),
            "messages": messages,
            "stream": False,
            "store": False,                       # PHI 비저장(헤더와 이중)
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if seed is not None:
            payload["options"]["seed"] = seed
        data = await self._post("/api/chat", payload, clinician_id=clinician_id)
        return extract_answer(data)

    async def warm(self, *, clinician_id: str, model: str | None = None,
                   keep_alive: str | None = None) -> None:
        """실패해도 예외를 던지지 않는다 — 워밍은 최적화이지 정확성 요건이 아니다."""
        try:
            await self._post("/api/models/warm", {
                "model": model or os.environ.get("CERNO_AI_MODEL", "qwen2.5:14b"),
                "keep_alive": keep_alive or os.environ.get("CERNO_AI_KEEP_ALIVE", "5m"),
            }, clinician_id=clinician_id)
        except Exception as e:
            log.info("워밍 실패(무시): %s", e)


def _record_queue_headers(r: httpx.Response) -> None:
    """AI 서버 공정 큐 대기 헤더를 관측에 반영 (v2.124.66).

    `X-Queue-Waited-Ms` 는 **0 도 명시**된다 — 헤더 부재("이 경로는 큐를 안 탐")와
    "대기 없음"을 구분하기 위해서다. 그래서 `is None` 으로 판별하고 falsy 로 하지 않는다.

    이 값이 `serving_busy` 보다 민감하다. 단일 키 직렬화는 429 를 내지 않고 조용히
    느려지므로 카운터에 안 잡힌다(06_동시성_실측.md).
    """
    raw = r.headers.get("X-Queue-Waited-Ms")
    if raw is None:
        return
    try:
        waited = float(raw)
    except ValueError:
        return

    def _int(name: str) -> int | None:
        v = r.headers.get(name)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    try:
        from ..obs import metrics
        metrics.record_queue(waited, _int("X-Queue-Position"), _int("X-Queue-Active"))
    except Exception:  # noqa: BLE001 — 관측이 요청을 깨뜨리지 않는다
        log.debug("큐 헤더 기록 실패", exc_info=True)


def _confidence(hit: dict) -> float | None:
    """히트에서 **코사인 유사도**를 꺼낸다. 없으면 None(판정 불가).

    🔴 모드마다 필드가 다르다 — 라이브 실측(2026-07-30):
        mode=vector   score=0.6909, confidence=null   ← 코사인이 score 에
        mode=hybrid   score=0.0167, confidence=0.6909 ← 코사인이 confidence 에
                                                        (score 는 RRF 순위 값)
    두 값이 정확히 일치하므로 임계 0.45 는 두 모드에 그대로 쓴다. 다만 **필드를 잘못
    읽으면 hybrid 에서 전건이 게이트에 걸려 아무 답도 못 한다** — confidence 우선.

    FTS-only 히트는 confidence 가 null 이며 이때 score(RRF)로 대체하면 안 된다.
    스케일이 달라 임계 비교가 무의미해진다. 판정 불가로 두고 호출부가 버린다.
    """
    c = hit.get("confidence")
    if c is not None:
        try:
            return float(c)
        except (TypeError, ValueError):
            return None
    s = hit.get("score")
    if s is None:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def extract_answer(data: dict) -> str:
    """`/api/chat` 응답에서 본문을 꺼낸다.

    🔴 **AI 서버 `/api/chat` 은 Ollama 형식이 아니다.** 라이브 실측(2026-07-30):
        {"answer": "...", "model_used": "...", "quality_check": {...},
         "routing_reason": "...", "domain_detected": "..."}
    Ollama 의 `message.content` / `response` 는 **없다**. 그걸 읽으면 200 OK 인데
    빈 문자열이 나와 "근거는 찾았는데 답이 없는" 상태가 된다 — edu 가 겪은
    "200 + 이형 페이로드" 와 같은 구조적 사각이다.

    폴백을 두는 이유: 서버가 경로를 바꾸거나(`/v1` 등) 래퍼가 달라질 수 있다.
    다만 **빈 문자열을 조용히 통과시키지 않는다** — 상위가 ungrounded 로 처리한다.
    """
    for key in ("answer", "response"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            return v
    msg = data.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), str):
        return msg["content"]
    # OpenAI 호환 경로
    for c in data.get("choices") or []:
        v = ((c or {}).get("message") or {}).get("content")
        if isinstance(v, str) and v.strip():
            return v
    return ""


def _retry_after(r: httpx.Response) -> float:
    for src in (r.headers.get("Retry-After"), _json_retry_after(r)):
        if src is None:
            continue
        try:
            v = float(src)
        except (TypeError, ValueError):
            continue
        if v >= 0:
            return v
    return 5.0


def _json_retry_after(r: httpx.Response) -> Any:
    try:
        return (r.json() or {}).get("retry_after")
    except ValueError:
        return None
