"""검수 접수 계약 테스트 (14 기획 §4 · 13 운영계획 §4).

핵심: 신원 없이는 안 된다 · 질의·답변 원문을 받지 않는다(스키마에 필드 자체가 없음) ·
오도·누락은 priority 로 표시된다 · 카운터가 오도율(중단 판단 입력)을 만든다.
"""
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

KEY = "test-api-key"
os.environ.setdefault("CERNO_API_KEY", KEY)
os.environ.setdefault("CERNO_AI_BASE", "https://ai.example")
os.environ.setdefault("CERNO_AI_KEY", "sk-test")

from fastapi.testclient import TestClient   # noqa: E402

import cerno.api.ask as ask_api              # noqa: E402
from cerno.api.main import app               # noqa: E402
from cerno.ask.log import ReviewLog          # noqa: E402
from cerno.obs import metrics                # noqa: E402


def headers(uid="12345", role="clinician"):
    exp = int(time.time()) + 60
    sig = hmac.new(KEY.encode(), f"{uid}.{role}.{exp}".encode(), hashlib.sha256).hexdigest()
    return {"X-Cerno-User": uid, "X-Cerno-Role": role,
            "X-Cerno-Sig": sig, "X-Cerno-Exp": str(exp)}


def review(**kw):
    base = {"correlation_id": "abc123", "usefulness": "helpful",
            "citation_ok": True, "missing": False}
    base.update(kw)
    return base


@pytest.fixture(autouse=True)
def _reset_metrics(tmp_path):
    metrics.reset()
    ask_api._review_log = ReviewLog(path=str(tmp_path / "review.db"))
    yield
    ask_api._review_log = None
    metrics.reset()


def test_review_requires_identity():
    assert TestClient(app).post("/review", json=review()).status_code == 401


def test_review_records_counters():
    c = TestClient(app)
    assert c.post("/review", json=review(), headers=headers()).status_code == 200
    r = c.post("/review", json=review(usefulness="misleading", citation_ok=False,
                                      missing=True, missing_note="금기 누락"),
               headers=headers())
    assert r.status_code == 200
    assert r.json()["priority"] is True          # 오도·누락은 즉시 공유 대상

    counters = metrics.snapshot()["counters"]
    assert counters["review_total"] == 2
    assert counters["review_misleading"] == 1
    assert counters["review_citation_bad"] == 1
    assert counters["review_missing"] == 1


def test_review_rejects_unknown_usefulness():
    r = TestClient(app).post("/review", json=review(usefulness="great"), headers=headers())
    assert r.status_code == 422


def test_review_note_length_capped():
    r = TestClient(app).post("/review", json=review(missing=True, missing_note="x" * 301),
                             headers=headers())
    assert r.status_code == 422              # 자유텍스트는 300자 상한 — 장문 PHI 유입 억제


def test_review_schema_has_no_answer_or_query_field():
    """계약 수준 보장: 원문을 보낼 필드 자체가 없다 — 보내면 무시가 아니라 구조가 막는다."""
    from cerno.api.ask import ReviewIn
    assert set(ReviewIn.model_fields) == {
        "correlation_id", "usefulness", "citation_ok", "missing", "missing_note"}


def test_review_persists_across_restart(tmp_path):
    """재기동(새 인스턴스)에도 남는다 — 카운터·컨테이너 로그와 달리 회고 원자료 (17 P0-1)."""
    c = TestClient(app)
    c.post("/review", json=review(), headers=headers())
    c.post("/review", json=review(usefulness="misleading", citation_ok=False,
                                  missing=True, missing_note="금기 누락"),
           headers=headers())
    path = ask_api._review_log._path
    reopened = ReviewLog(path=path)              # 재기동 재현: 같은 파일, 새 인스턴스
    t = reopened.tally()
    assert t == {"total": 2, "misleading": 1, "citation_bad": 1, "missing": 1}


def test_review_storage_failure_is_not_silent():
    """저장 실패 = 접수 실패(503). 조용한 성공 처리는 수집 유실을 숨긴다."""
    class Broken:
        def record(self, **kw):
            raise RuntimeError("disk full")
    ask_api._review_log = Broken()
    r = TestClient(app).post("/review", json=review(), headers=headers())
    assert r.status_code == 503
    assert metrics.snapshot()["counters"].get("review_total", 0) == 0   # 실패는 집계 안 됨
