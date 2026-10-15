"""환자별 질의 로그 — 보관·재활용 (2026-08-04 사용자 결정).

⚠️ **PHI 비저장 경계의 명시적 예외다.** 원래 질의·답변은 브라우저 밖에 저장하지
않았으나(10_보안점검), 의료진이 환자별 질의 이력을 세션을 넘어 재활용할 수 있도록
보관하기로 했다. 완화 조건:
  - 조회는 **작성한 의료진 본인 것만** — clinician_id 는 검증된 신원에서만 온다.
    (최악 시나리오인 타 의료진 노출을 막는 1차 격리. 서고 private 와 같은 원칙)
  - 파일럿/규제 검토 시 보존 기간·암호화·감사로그를 재평가한다.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass

_DDL = """
CREATE TABLE IF NOT EXISTS ask_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    clinician_id   TEXT NOT NULL,
    patient_id     TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    query          TEXT NOT NULL,
    answer_json    TEXT NOT NULL,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_asklog_owner_patient
    ON ask_log(clinician_id, patient_id, created_at);
"""

_REVIEW_DDL = """
CREATE TABLE IF NOT EXISTS review_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id TEXT NOT NULL,
    clinician_id   TEXT NOT NULL,
    usefulness     TEXT NOT NULL,
    citation_ok    INTEGER,
    missing        INTEGER NOT NULL,
    missing_note   TEXT NOT NULL DEFAULT '',
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reviewlog_created ON review_log(created_at);
"""


@contextmanager
def _conn(path: str):
    c = sqlite3.connect(path, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    try:
        yield c
        c.commit()
    finally:
        c.close()


@dataclass(frozen=True)
class AskLogEntry:
    query: str
    answer: dict
    correlation_id: str
    created_at: float


class AskLog:
    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get("CERNO_DB_PATH", "cerno.db")
        with self._conn() as c:
            c.executescript(_DDL)

    def _conn(self):
        return _conn(self._path)

    def record(self, *, clinician_id: str, patient_id: str,
               correlation_id: str, query: str, answer: dict) -> None:
        with self._conn() as c:
            c.execute(
                "INSERT INTO ask_log (clinician_id, patient_id, correlation_id,"
                " query, answer_json, created_at) VALUES (?,?,?,?,?,?)",
                (clinician_id, patient_id, correlation_id,
                 query, json.dumps(answer, ensure_ascii=False), time.time()))

    def for_patient(self, *, clinician_id: str, patient_id: str,
                    limit: int = 50) -> list[AskLogEntry]:
        """본인(clinician_id) 기록만. 오래된 것부터 — 화면 레일 순서와 일치."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT query, answer_json, correlation_id, created_at FROM ask_log"
                " WHERE clinician_id=? AND patient_id=?"
                " ORDER BY created_at DESC LIMIT ?",
                (clinician_id, patient_id, limit)).fetchall()
        out = []
        for r in reversed(rows):
            try:
                ans = json.loads(r["answer_json"])
            except ValueError:
                continue
            out.append(AskLogEntry(query=r["query"], answer=ans,
                                   correlation_id=r["correlation_id"],
                                   created_at=r["created_at"]))
        return out


class ReviewLog:
    """의료진 검수 기록 영속화 (17 기획 P0-1).

    카운터·컨테이너 로그는 재기동에 유실된다 — 오도율(파일럿 중단 판단 입력)과
    회고 대조의 원자료이므로 ask_log 와 같은 볼륨에 남긴다. 원문·환자 정보는
    처음부터 수신하지 않으므로(ReviewIn 계약) PHI 경계 변화가 없다.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get("CERNO_DB_PATH", "cerno.db")
        with _conn(self._path) as c:
            c.executescript(_REVIEW_DDL)

    def record(self, *, correlation_id: str, clinician_id: str, usefulness: str,
               citation_ok: bool | None, missing: bool, missing_note: str) -> None:
        with _conn(self._path) as c:
            c.execute(
                "INSERT INTO review_log (correlation_id, clinician_id, usefulness,"
                " citation_ok, missing, missing_note, created_at)"
                " VALUES (?,?,?,?,?,?,?)",
                (correlation_id, clinician_id, usefulness,
                 None if citation_ok is None else int(citation_ok),
                 int(missing), missing_note, time.time()))

    def tally(self) -> dict:
        """회고용 집계 — 재기동과 무관한 전 기간 값."""
        with _conn(self._path) as c:
            r = c.execute(
                "SELECT COUNT(*) AS total,"
                " SUM(usefulness='misleading') AS misleading,"
                " SUM(citation_ok=0) AS citation_bad,"
                " SUM(missing) AS missing FROM review_log").fetchone()
        return {"total": r["total"], "misleading": r["misleading"] or 0,
                "citation_bad": r["citation_bad"] or 0, "missing": r["missing"] or 0}
