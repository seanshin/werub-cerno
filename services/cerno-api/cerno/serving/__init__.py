"""서빙 어댑터 — AI 서버 호출의 유일한 출구.

파이프라인 어디서도 Ollama·heavy·/api/chat 을 직접 부르지 않는다. 전부 여기를 경유한다.
GPU 증설 후 vLLM 으로 전환할 때 **이 패키지만** 교체하면 되도록 격리하는 것이 목적이다.
(01_구현계획.md §6.1 — 파킹된 LoRA 를 재작성 없이 붙이기 위한 유일한 선결 조건)

실측 근거(02_Phase0_실측결과.md):
  - 콜드 로드 2.9s / warm 생성 0.58~0.68s / embed warm 0.15s
  - MAX_LOADED_MODELS 실효값 **2** → 14b + bge-m3 공존, RAG 사이클 스왑 0

경로(AI 서버 회신 A-3 반영): `POST /api/chat` + `POST /api/models/warm`.
~~heavy 레인~~ 은 쓰지 않는다 — 현재 비활성(`heavy_enabled=false`)이고 `heavy_model`
(2026-08-03 현재 `gemma4:26b` — AI 서버 디스크 정리로 `qwen3.6:35b-a3b` 에서 교체)이
기본 채팅과 공존 불가한 크기라 켜는 순간 상주 모델을 축출해 VRAM 예산이 깨진다.
single-flight 이 필요한 이유(대형 모델 동시 적재 OOM)가 14b 공유에는 없다.
상주 정책 실측 기준선: AI 서버 `docs/DESIGN_MODEL_SERVING.md`.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capabilities:
    """상위 코드가 분기 판단에 쓰는 런타임 능력."""

    multi_adapter: bool = False
    """True 면 의료진별 어댑터를 요청 단위로 교체할 수 있다(vLLM multi-LoRA).
    False 인 현 Ollama 스택에서는 의료진 전환이 전체 모델 콜드 스왑이므로
    personal.strategy() 가 'prompt' 여야 한다."""

    streaming: bool = True
    max_context: int = 8192
    """OLLAMA_NUM_PARALLEL=2 하에서 슬롯당 컨텍스트. 초과분은 조용히 절단되므로
    호출 전 상위에서 근거 분량을 제한할 것."""


@dataclass(frozen=True)
class GenParams:
    temperature: float = 0.2
    seed: int | None = 42
    """재현성 확보용 고정 seed. 변경은 성능 기준선 재산출을 요구한다.
    ⚠️ 결정성의 수단은 seed 이지 greedy(temperature=0)가 아니다 —
    temperature=0 은 한국어 붕괴·JSON 미닫힘을 유발한 전례가 있다(twin v1.20.52)."""
    num_predict: int = 800
    stop: tuple[str, ...] = ()
    extra: dict = field(default_factory=dict)


class ServingBackend(ABC):
    """모든 생성·임베딩·워밍의 단일 진입점."""

    @abstractmethod
    def capabilities(self) -> Capabilities: ...

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        *,
        clinician_id: str,
        system: str | None = None,
        params: GenParams | None = None,
    ) -> str:
        """근거가 포함된 프롬프트로 답변 생성.

        ★ clinician_id 는 **동시성에 직결된다.** AI 서버 공정 큐(`ChatFairQueue`)의
        버킷 키가 **API 키의 sha256** 이라, 단일 키로 호출하면 의료진이 몇 명이든
        `max_per_user=1` 에 걸려 직렬화된다. 구현체는 clinician_id 로 **의료진별 키**를
        선택해야 한다(AI 서버 회신 A-1, 택1 중 A안).

        Raises:
            ServingBusy: 공정 큐 대기 초과(429 + retry_after). 최대 대기 60초 후 발생.
            ServingUnavailable: 업스트림 장애. 상위는 안전 응답으로 degrade 한다.
        """

    @abstractmethod
    async def warm(self, *, clinician_id: str, ttl_seconds: int) -> None:
        """진료 세션 시작 시 선제 적재. 첫 질의가 콜드(~2.9s)를 맞지 않게 한다.

        실패해도 예외를 던지지 않는다 — 워밍은 최적화이지 정확성 요건이 아니다.
        """

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """검색용 임베딩. RAG 검색을 AI 서버에 위임하는 경우 직접 호출하지 않을 수 있다."""


class ServingBusy(RuntimeError):
    """AI 서버 공정 큐 대기 초과(429).

    실측 상한: `max_per_user=1`(버킷=API 키) · `max_global=3` · 대기열 20 · 대기 60초.
    **`max_global=3` 이 진짜 천장**이며 의료진별 키를 써도 4번째 동시 요청은 대기한다.
    이 예외의 발생률이 곧 GPU 증설 판단의 1차 지표다.
    """

    def __init__(self, retry_after: int = 10):
        super().__init__(f"serving busy, retry after {retry_after}s")
        self.retry_after = retry_after


class ServingUnavailable(RuntimeError):
    """업스트림 장애. 근거 없이 생성하지 말고 안전 응답으로 degrade 할 것."""
