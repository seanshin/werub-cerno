"""문서 등록부 — 매니페스트 영속화 (SQLite).

**PHI 는 여기 들어오지 않는다.** 본문은 저장하지 않고 매니페스트(메타)와 적재 결과만
남긴다. 본문은 청킹 직후 AI 서버로 흘러가고 Cerno 프로세스 메모리를 벗어나지 않는다.

등록부가 필요한 이유는 둘이다.
  1. **삭제** — 청크 id 를 재구성하려면 chunk_count 와 groups 를 알아야 한다.
     벡터DB 만 보고는 "이 문서의 청크가 몇 개였나"를 알 수 없다.
  2. **목록·감사** — 누가 무엇을 어떤 공개범위로 올렸는지.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

from .manifest import Manifest, Source, parse

_DDL = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id        TEXT PRIMARY KEY,
    owner         TEXT NOT NULL,
    visibility    TEXT NOT NULL,
    title         TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    chunk_count   INTEGER NOT NULL DEFAULT 0,
    groups_json   TEXT NOT NULL DEFAULT '[]',
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_documents_owner ON documents(owner);
CREATE INDEX IF NOT EXISTS idx_documents_visibility ON documents(visibility);
"""


@dataclass(frozen=True)
class DocRecord:
    doc_id: str
    owner: str
    visibility: str
    title: str
    chunk_count: int
    groups: list[str]
    created_at: float
    updated_at: float

    def manifest(self, raw: dict) -> Manifest:
        return parse(raw)


class DocumentStore:
    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get("CERNO_DB_PATH", "cerno.db")
        with self._conn() as c:
            c.executescript(_DDL)

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self._path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def upsert(self, m: Manifest, *, chunk_count: int) -> None:
        now = time.time()
        payload = json.dumps(_to_dict(m), ensure_ascii=False)
        with self._conn() as c:
            c.execute(
                """INSERT INTO documents
                     (doc_id, owner, visibility, title, manifest_json, chunk_count,
                      groups_json, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(doc_id) DO UPDATE SET
                     owner=excluded.owner, visibility=excluded.visibility,
                     title=excluded.title, manifest_json=excluded.manifest_json,
                     chunk_count=excluded.chunk_count, groups_json=excluded.groups_json,
                     updated_at=excluded.updated_at""",
                (m.doc_id, m.owner_clinician_id, m.visibility, m.title, payload,
                 chunk_count, json.dumps(m.groups), now, now),
            )

    def get(self, doc_id: str) -> tuple[DocRecord, dict] | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM documents WHERE doc_id=?", (doc_id,)).fetchone()
        if not r:
            return None
        return _rec(r), json.loads(r["manifest_json"])

    def list_visible(self, *, owner: str, groups: list[str]) -> list[DocRecord]:
        """포털 목록 — **접근 가능한 것만.** 검색과 동일한 격리 규칙을 적용한다.

        여기서 규칙이 어긋나면 검색으로는 못 보는 문서를 포털에서 보게 된다.
        """
        q = ["SELECT * FROM documents WHERE (visibility='org' OR (visibility='private' AND owner=?)"]
        args: list[object] = [owner]
        if groups:
            marks = ",".join("?" * len(groups))
            q.append(f" OR (visibility='group' AND EXISTS ("
                     f"SELECT 1 FROM json_each(groups_json) WHERE value IN ({marks})))")
            args.extend(groups)
        q.append(") ORDER BY updated_at DESC")
        with self._conn() as c:
            rows = c.execute("".join(q), args).fetchall()
        return [_rec(r) for r in rows]

    def delete(self, doc_id: str) -> bool:
        with self._conn() as c:
            cur = c.execute("DELETE FROM documents WHERE doc_id=?", (doc_id,))
        return cur.rowcount > 0


def _rec(r: sqlite3.Row) -> DocRecord:
    return DocRecord(
        doc_id=r["doc_id"], owner=r["owner"], visibility=r["visibility"],
        title=r["title"], chunk_count=r["chunk_count"],
        groups=json.loads(r["groups_json"]),
        created_at=r["created_at"], updated_at=r["updated_at"],
    )


def _to_dict(m: Manifest) -> dict:
    s: Source = m.source
    return {
        "doc_id": m.doc_id, "title": m.title, "visibility": m.visibility,
        "owner_clinician_id": m.owner_clinician_id, "source_type": m.source_type,
        "files": m.files, "groups": m.groups,
        "source": {"authors": s.authors, "publication": s.publication, "year": s.year,
                   "doi": s.doi, "url": s.url, "license": s.license},
        "phi_reviewed": m.phi_reviewed, "license_approved": m.license_approved,
        "specialty": m.specialty, "modalities": m.modalities, "icd_codes": m.icd_codes,
    }
