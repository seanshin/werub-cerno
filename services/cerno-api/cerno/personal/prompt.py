"""무어댑터 개인화 — 프롬프트 전략 (Phase 4-A).

LoRA 가 파킹된 현 스택의 개인화 수단이다(01_구현계획.md §7).
  1. 의료진별 시스템 프롬프트 — 설명 방식·강조점·서술 순서
  2. 개인 서고 가중 — 본인 자료가 근거로 우선 인용되게
  3. few-shot — **config 에 큐레이션한** 답변 예시 (검색이 아니다 · `apply()` 주석 참조)

증설 + vLLM 전환 시 `strategy()` 가 "adapter" 인 구현체로 교체하면 상위 코드는
바뀌지 않는다. 그것이 이 계층을 둔 이유다.
"""
from __future__ import annotations

import functools
import hashlib
import os
from typing import Sequence

import yaml

from . import Evidence, PersonalizationBackend, PersonalizedPrompt, Strategy
from .sanitize import sanitize_data

_CONFIG_DIR = os.environ.get("CERNO_CONFIG_DIR", "config")

_VIS_LABEL = {"private": "개인 자료", "group": "그룹 자료", "org": "공용 자료"}

DEFAULT_WEIGHTS = {"private": 1.3, "group": 1.1, "org": 1.0}


@functools.lru_cache(maxsize=1)
def _registry() -> dict:
    path = os.path.join(_CONFIG_DIR, "prompts", "registry.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


@functools.lru_cache(maxsize=1)
def _clinicians() -> dict:
    path = os.path.join(_CONFIG_DIR, "clinicians.yaml")
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except FileNotFoundError:
        return {}


def _profile(clinician_id: str) -> dict:
    for c in _clinicians().get("clinicians") or []:
        if str(c.get("id")) == clinician_id:
            return c
    return {}


class PromptPersonalization(PersonalizationBackend):
    def strategy(self) -> Strategy:
        return "prompt"

    def evidence_weight(self, *, clinician_id: str, visibility: str) -> float:
        w = (_clinicians().get("defaults") or {}).get("evidence_weight") or DEFAULT_WEIGHTS
        prof = _profile(clinician_id).get("evidence_weight") or {}
        return float(prof.get(visibility, w.get(visibility, 1.0)))

    def weights(self, clinician_id: str) -> dict[str, float]:
        return {v: self.evidence_weight(clinician_id=clinician_id, visibility=v)
                for v in ("private", "group", "org")}

    def apply(
        self,
        question: str,
        *,
        clinician_id: str,
        evidence: Sequence[Evidence],
        patient_context: str | None = None,
    ) -> PersonalizedPrompt:
        reg = _registry()
        version = (_profile(clinician_id).get("prompt_version")
                   or (reg.get("defaults") or {}).get("prompt_version")
                   or reg.get("base") or "cerno.narrative@1.0.0")
        spec = (reg.get("prompts") or {}).get(version) or {}
        system = spec.get("system", "").strip()

        # 의료진 개인 스타일 — 정본 지시를 덮어쓰지 않도록 **뒤에** 덧붙인다.
        # 안전 규칙(근거 강제·한정 조건 보존)이 개인 취향으로 무력화되면 안 된다.
        style = (_profile(clinician_id).get("style_notes") or "").strip()
        if style:
            system = f"{system}\n\n[이 의료진의 설명 방식]\n{style}"

        blocks: list[str] = []

        # few-shot — 이 의료진의 서술 방식 예시 (Phase 4-A3).
        #
        # ★ 설계 판단: **검색으로 뽑지 않고 config 에 큐레이션한다.**
        #   ① 규제 추적성 — few-shot 도 프롬프트 내용이다. 매 요청 검색으로 채우면
        #      실제 전송된 프롬프트가 요청마다 달라져 `prompt_version` 이 무엇이
        #      보내졌는지 식별하지 못한다. twin 의 프롬프트 형상관리 교훈과 충돌한다.
        #   ② 동시성 — 검색 1회가 추가되면 그만큼 공정 큐 버킷을 더 잡는다.
        #      단일 키 직렬화 실측(06_동시성_실측.md)에서 요청당 ~1.16초가 붙는다.
        #   ③ 품질 — 주제로 검색한 개인 문서는 "스타일 예시" 가 아니라 근거다.
        #      이미 [근거] 에 들어가 있어 중복이 된다.
        shots = self.few_shot(clinician_id)
        if shots:
            # few-shot 은 config 큐레이션이라 신뢰 입력이나, 구조 위조는 여기서도 막는다
            lines = [f"Q. {sanitize_data(s['q'])}\nA. {sanitize_data(s['a'])}" for s in shots]
            blocks.append(
                "[이 의료진의 답변 예시]\n"
                "형식과 어조만 참고한다. 예시의 **내용을 사실로 쓰지 않는다** — "
                "답변 근거는 아래 [근거] 뿐이다.\n\n" + "\n\n".join(lines))

        if patient_context:
            blocks.append(f"[환자 맥락]\n{sanitize_data(patient_context)}")

        # 🔴 근거·인용은 신뢰할 수 없는 입력이다(의료진 업로드·FHIR 자유텍스트).
        # 데이터가 섹션 헤더를 흉내내 블록 경계를 위조하는 것을 sanitize 로 차단한다.
        ev_lines = []
        for e in evidence:
            label = _VIS_LABEL.get(e.visibility, e.visibility)
            cite = sanitize_data(e.citation)
            text = sanitize_data(e.text)
            ev_lines.append(f"[{e.rank}] ({label} · {cite})\n{text}")
        blocks.append("[근거]\n" + "\n\n".join(ev_lines))
        blocks.append(f"[질문]\n{sanitize_data(question)}")

        return PersonalizedPrompt(
            system=system,
            prompt="\n\n".join(blocks),
            # 프로파일(스타일·few-shot)이 바뀌면 실제 전송 프롬프트가 달라진다.
            # 지문을 버전에 붙여 "무엇이 보내졌나" 를 사후에 특정할 수 있게 한다.
            prompt_version=f"{version}+p{self.profile_fingerprint(clinician_id)}",
            adapter_id=None,        # strategy()=="prompt" 이므로 항상 None
        )

    # ── 프로파일 ────────────────────────────────────────────

    def few_shot(self, clinician_id: str) -> list[dict]:
        """큐레이션된 답변 예시. 상한을 둔다 — 예시가 근거를 밀어내면 본말전도다."""
        k = int((_clinicians().get("defaults") or {}).get("few_shot_k", 2))
        raw = _profile(clinician_id).get("few_shot") or []
        out = []
        for s in raw:
            if isinstance(s, dict) and s.get("q") and s.get("a"):
                out.append({"q": str(s["q"]).strip(), "a": str(s["a"]).strip()})
        return out[:max(0, k)]

    def profile_fingerprint(self, clinician_id: str) -> str:
        """스타일·few-shot 의 짧은 해시. 프로파일이 없으면 '0'."""
        prof = _profile(clinician_id)
        material = str(prof.get("style_notes") or "") + repr(self.few_shot(clinician_id))
        if not material.strip() or material == "[]":
            return "0"
        return hashlib.sha256(material.encode()).hexdigest()[:8]
