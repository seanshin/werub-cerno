"""HIS FHIR 근거 조회 커넥터 — 읽기 전용 · per-user 토큰 · PHI 비저장.

01_구현계획.md Phase 1-6.

원칙 셋을 코드로 강제한다.
  1. **읽기 전용** — GET 외 메서드를 노출하지 않는다. Cerno 는 HIS 에 write-back 하지 않는다.
  2. **의료진 본인 권한** — Backend Services(M2M)가 아니라 EHR launch 로 받은 per-user
     토큰을 쓴다. 조회 범위가 그 의료진 권한을 넘지 못한다.
  3. **PHI 비저장** — 응답은 반환값으로만 흐른다. 디스크·로그에 원문을 남기지 않는다.

degrade 규약:
  - 토큰 없음/만료 → `SmartReentryRequired` (재인증 요구, 오류 아님)
  - HIS 장애·서킷 열림 → `EvidenceUnavailable` → 상위는 "근거 조회 불가"로 안전 응답
  둘 다 **추측으로 메우지 않는다.**
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

import httpx

from ..circuit import CircuitOpen, breaker
from .tokens import SmartReentryRequired, SmartToken, get_smart_token

log = logging.getLogger("cerno.fhir")

__all__ = [
    "FhirClient", "EvidenceUnavailable", "SmartReentryRequired",
    "REQUIRED_RESOURCES", "RECOMMENDED_RESOURCES",
]


class EvidenceUnavailable(Exception):
    """HIS 조회 실패. 상위는 근거 없이 생성하지 말고 안전 응답으로 degrade 한다."""


# HIS 가드 교체(feat/fhir-smart-guard-3resources) 전에는 뒤 3종이 401 을 반환한다.
# 그 경우에도 전체가 실패하지 않도록 리소스별로 독립 수집한다(부분 실패 허용).
REQUIRED_RESOURCES = ("Condition", "Observation", "DiagnosticReport", "Procedure")
RECOMMENDED_RESOURCES = ("MedicationRequest", "AllergyIntolerance", "DocumentReference")

_DEFAULT_COUNT = 20
_TIMEOUT = float(os.environ.get("CERNO_FHIR_TIMEOUT", "10"))


def _retry_after_s(r: httpx.Response) -> float | None:
    """429 의 Retry-After(초). 정수 초만 지원하고 HTTP-date·음수는 무시한다."""
    v = r.headers.get("Retry-After")
    if not v:
        return None
    try:
        s = float(v)
    except ValueError:
        return None
    return s if s >= 0 else None


class FhirClient:
    """의료진 1인의 조회 세션. 요청마다 생성하고 재사용하지 않는다(토큰이 사람마다 다름)."""

    def __init__(
        self,
        token: SmartToken,
        *,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base = (base_url or os.environ.get("CERNO_FHIR_BASE") or "").rstrip("/")
        if not self._base:
            raise RuntimeError("CERNO_FHIR_BASE is required")
        self._transport = transport   # 테스트 주입용. 운영에서는 None

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_TIMEOUT, transport=self._transport)

    @classmethod
    async def for_clinician(cls, clinician_id: str, **kw) -> "FhirClient":
        return cls(await get_smart_token(clinician_id), **kw)

    @property
    def launch_patient(self) -> str:
        """launch 컨텍스트의 환자. standalone launch 면 빈 문자열."""
        return self._token.patient

    # ── 내부 ────────────────────────────────────────────────

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> Any:
        """서킷브레이커 + 429 백오프. 서킷은 429 에 트립하지 않는다(서버 장애 아님)."""
        br = breaker("his")
        if not br.allow():
            raise CircuitOpen("HIS 서킷 열림(연속 실패 차단)")

        headers = {
            "Authorization": f"Bearer {self._token.access_token}",
            "Accept": "application/fhir+json",
        }
        try:
            for attempt in range(2):
                r = await client.get(f"{self._base}/{path}", params=params, headers=headers)
                if r.status_code != 429:
                    break
                wait = _retry_after_s(r) or (0.5 * (attempt + 1))
                # HIS rate limit(≈600/min) 순간 초과 — transient 로 흡수
                log.info("HIS 429 → %.1fs 후 재시도(%d/2)", wait, attempt + 1)
                await asyncio.sleep(wait)

            if r.status_code == 401:
                # 토큰 만료·스코프 부족. 서킷 트립 대상이 아니다.
                raise SmartReentryRequired(f"HIS 401 ({path})")
            if r.status_code == 403:
                # 가드/스코프 문제 — 재인증으로 안 풀린다. 해당 리소스만 포기한다.
                raise EvidenceUnavailable(f"HIS 403 ({path})")
            r.raise_for_status()
        except (SmartReentryRequired, EvidenceUnavailable):
            raise
        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                br.record_failure()
            raise EvidenceUnavailable(f"HIS {e.response.status_code} ({path})") from None
        except httpx.HTTPError:
            br.record_failure()
            # 예외 원문에 URL·내부 경로가 실릴 수 있어 전파하지 않는다
            raise EvidenceUnavailable(f"HIS 통신 실패 ({path})") from None

        br.record_success()
        return r.json()

    @staticmethod
    def _entries(bundle: Any) -> list[dict]:
        if not isinstance(bundle, dict):
            return []
        return [e["resource"] for e in bundle.get("entry") or [] if isinstance(e, dict) and e.get("resource")]

    # ── 공개 API (읽기 전용) ────────────────────────────────

    async def patient_context(
        self,
        patient_id: str,
        *,
        resources: tuple[str, ...] | None = None,
        count: int = _DEFAULT_COUNT,
    ) -> dict[str, list[dict]]:
        """환자 근거 묶음. 리소스별 **독립 수집**이라 일부 실패해도 나머지는 살린다.

        HIS 가드 교체 전에는 MedicationRequest·AllergyIntolerance·DocumentReference 가
        401/403 을 반환하므로, 전체 실패로 만들지 않고 해당 키를 비워 둔다.
        상위(safety)는 비어 있음을 **근거 없음**으로 다뤄야 하며 추측으로 메우면 안 된다.
        """
        if not patient_id:
            raise EvidenceUnavailable("patient_id 없음")

        wanted = resources or (REQUIRED_RESOURCES + RECOMMENDED_RESOURCES)
        out: dict[str, list[dict]] = {}

        async with self._client() as client:
            async def one(rt: str) -> tuple[str, list[dict]]:
                params: dict[str, Any] = {"_count": count}
                # HIS 컨트롤러는 Condition 이 subject, 나머지는 patient 를 받는다
                params["subject" if rt == "Condition" else "patient"] = patient_id
                try:
                    return rt, self._entries(await self._get(client, rt, params))
                except SmartReentryRequired:
                    raise                     # 토큰 문제는 전체에 해당하므로 올린다
                except (EvidenceUnavailable, CircuitOpen) as e:
                    log.info("FHIR %s 수집 생략: %s", rt, e)
                    return rt, []

            async def demographics() -> tuple[str, list[dict]]:
                # 성별·연령 근거 — 없으면 추천·안전 로직이 환자에 안 맞는 주제
                # (예: 남성에게 임신)를 걸러낼 수 없다. 인적사항(이름·번호)은
                # context.build 가 버린다.
                try:
                    return "Patient", [await self._get(client, f"Patient/{patient_id}", {})]
                except SmartReentryRequired:
                    raise
                except (EvidenceUnavailable, CircuitOpen) as e:
                    log.info("FHIR Patient 수집 생략: %s", e)
                    return "Patient", []

            tasks = [one(rt) for rt in wanted] + [demographics()]
            for rt, entries in await asyncio.gather(*tasks):
                out[rt] = entries

        return out

    async def patient(self, patient_id: str) -> dict | None:
        async with self._client() as client:
            try:
                return await self._get(client, f"Patient/{patient_id}", {})
            except (EvidenceUnavailable, CircuitOpen):
                return None
