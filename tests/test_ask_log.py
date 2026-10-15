"""환자별 질의 로그(ask/log.py + GET /ask/history) — PHI 경계 예외의 격리 계약.

핵심: **본인 기록만 조회된다.** 타 의료진 기록은 쿼리가 닿지 않는다.
보관 실패는 답변을 막지 않는다(엔드포인트 쪽 계약).
"""
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

KEY = "test-api-key"
os.environ.setdefault("CERNO_API_KEY", KEY)
os.environ.setdefault("CERNO_AI_BASE", "https://ai.example")
os.environ.setdefault("CERNO_AI_KEY", "sk-test")

from fastapi.testclient import TestClient   # noqa: E402

import cerno.api.ask as ask_api              # noqa: E402
from cerno.api.main import app               # noqa: E402
from cerno.ask.log import AskLog             # noqa: E402


def headers(uid: str) -> dict:
    exp = int(time.time()) + 30
    sig = hmac.new(KEY.encode(), f"{uid}.clinician.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    return {"X-Cerno-User": uid, "X-Cerno-Role": "clinician",
            "X-Cerno-Exp": str(exp), "X-Cerno-Sig": sig}


def _store(tmp_path) -> AskLog:
    return AskLog(path=str(tmp_path / "asklog.db"))


def test_only_own_entries_come_back(tmp_path):
    s = _store(tmp_path)
    s.record(clinician_id="doc-A", patient_id="P-1", correlation_id="c1",
             query="A의 질문", answer={"answer": "a", "grounded": True})
    s.record(clinician_id="doc-B", patient_id="P-1", correlation_id="c2",
             query="B의 질문", answer={"answer": "b", "grounded": True})
    mine = s.for_patient(clinician_id="doc-A", patient_id="P-1")
    assert [e.query for e in mine] == ["A의 질문"]


def test_scoped_by_patient_and_ordered_oldest_first(tmp_path):
    s = _store(tmp_path)
    for i, pid in enumerate(["P-1", "P-2", "P-1"]):
        s.record(clinician_id="doc-A", patient_id=pid, correlation_id=f"c{i}",
                 query=f"q{i}", answer={"answer": "x"})
    got = s.for_patient(clinician_id="doc-A", patient_id="P-1")
    assert [e.query for e in got] == ["q0", "q2"]      # 오래된 것부터


def test_corrupt_answer_row_is_skipped(tmp_path):
    s = _store(tmp_path)
    s.record(clinician_id="doc-A", patient_id="P-1", correlation_id="c1",
             query="q", answer={"answer": "ok"})
    with s._conn() as c:
        c.execute("UPDATE ask_log SET answer_json='{깨진' WHERE correlation_id='c1'")
    assert s.for_patient(clinician_id="doc-A", patient_id="P-1") == []


def test_history_endpoint_isolated_per_identity(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.record(clinician_id="doc-A", patient_id="P-1", correlation_id="c1",
             query="A만 보여야 함", answer={"answer": "a"})
    monkeypatch.setattr(ask_api, "get_ask_log", lambda: s)
    c = TestClient(app)
    r_a = c.get("/ask/history", params={"patient_id": "P-1"}, headers=headers("doc-A"))
    r_b = c.get("/ask/history", params={"patient_id": "P-1"}, headers=headers("doc-B"))
    assert [e["query"] for e in r_a.json()["entries"]] == ["A만 보여야 함"]
    assert r_b.json()["entries"] == []


def test_history_requires_identity_and_patient(tmp_path, monkeypatch):
    monkeypatch.setattr(ask_api, "get_ask_log", lambda: _store(tmp_path))
    c = TestClient(app)
    assert c.get("/ask/history", params={"patient_id": "P-1"}).status_code == 401
    assert c.get("/ask/history", params={"patient_id": "  "},
                 headers=headers("doc-A")).status_code == 400
