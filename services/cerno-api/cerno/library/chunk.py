"""문서 청킹 — 한국어 임상 문서 기준.

AI 서버 `/api/rag/index` 의 **배치 경로는 청킹을 하지 않는다**(단건 경로만 `chunk_strategy`
지원). 배치가 필수인 이상(단건 1.47s/건 vs 배치 ~12ms/건) Cerno 가 직접 쪼갠다.

⚠️ 청크 id 규약을 `ask/search.py::parent_doc_id` 와 **반드시 맞춰야 한다.**
어긋나면 중복 제거가 동작하지 않아 한 문서가 상위 슬롯을 잠식한다.
"""
from __future__ import annotations

import re

DEFAULT_MAX_CHARS = 500
DEFAULT_OVERLAP = 50

# 문단 → 문장 순으로 자른다. 한국어 종결어미(다./요.)와 마침표·물음표·느낌표.
_PARA = re.compile(r"\n\s*\n")
_SENT = re.compile(r"(?<=[.!?。])\s+|(?<=[다요])\.\s+")


def _split_sentences(text: str) -> list[str]:
    parts = [s.strip() for s in _SENT.split(text) if s and s.strip()]
    return parts or ([text.strip()] if text.strip() else [])


def chunk_text(
    text: str,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[str]:
    """문단 경계를 우선 존중하고, 넘치면 문장 단위로 쪼갠다.

    한 문장이 `max_chars` 를 넘으면 그 문장은 그대로 둔다 — 문장 중간을 자르면
    임상 문장의 의미가 뒤집힐 수 있다(예: 금기·용량 단서가 잘림). 길이보다 온전함이 낫다.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    units: list[str] = []
    for para in _PARA.split(text):
        para = para.strip()
        if not para:
            continue
        if len(para) <= max_chars:
            units.append(para)
        else:
            units.extend(_split_sentences(para))

    chunks: list[str] = []
    buf = ""
    for u in units:
        if not buf:
            buf = u
        elif len(buf) + 1 + len(u) <= max_chars:
            buf = f"{buf} {u}"
        else:
            chunks.append(buf)
            # 겹침 — 경계에서 문맥이 끊기는 것을 완화한다
            tail = buf[-overlap:] if overlap > 0 else ""
            buf = f"{tail} {u}".strip() if tail else u
    if buf:
        chunks.append(buf)
    return chunks


def chunk_id(doc_id: str, index: int, *, group: str | None = None) -> str:
    """청크 id. `parent_doc_id` 가 되돌릴 수 있는 형태여야 한다.

    형식: `{doc_id}[__grp_{group}]__chunk_{n}`
    group 접미는 다중 그룹 문서를 그룹별로 색인할 때만 붙는다(§ingest 전개).
    """
    base = f"{doc_id}__grp_{group}" if group else doc_id
    return f"{base}__chunk_{index}"
