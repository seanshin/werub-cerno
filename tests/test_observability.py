"""관측성 계약 테스트 (Phase 3-6).

로그 스크러빙은 twin 아키텍처 감사의 **유일 BLOCKER** 였다. 관찰성을 끄지 않고
식별자만 지운다 — 로그를 없애면 사고 조사가 불가능해지고 그건 다른 종류의 위험이다.
"""
import logging
import sys

import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

from cerno.obs import metrics
from cerno.obs.scrub import REDACT, MessageScrubFilter, hash_id, scrub


# ── 스크러빙 ────────────────────────────────────────────────

def test_fhir_patient_reference_masked():
    assert scrub("조회 Patient/abc-123 완료") == f"조회 Patient/{REDACT} 완료"


def test_practitioner_reference_masked():
    """clinician_id 는 개인 서고의 소유 키다 — 평문으로 남으면 '누가 무엇을 물었나'가
    재구성된다. twin 에는 없던 Cerno 고유 보호 대상."""
    assert REDACT in scrub("fhirUser=Practitioner/12345")
    assert "12345" not in scrub("fhirUser=Practitioner/12345")


def test_patient_path_masked():
    assert scrub("GET /patient/P998 200") == f"GET /patient/{REDACT} 200"


def test_uuid_masked():
    u = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    assert u not in scrub(f"resource {u} fetched")


def test_korean_rrn_masked():
    assert "901010-1234567" not in scrub("주민 901010-1234567 확인")


def test_phone_masked():
    assert "010-1234-5678" not in scrub("연락처 010-1234-5678")


def test_api_key_truncated_not_fully_shown():
    """사고 조사 중 헤더를 덤프하는 일이 실제로 있다."""
    out = scrub("Authorization: Bearer sk-EXAMPLE0deadbeef0deadbeef0cafef00d")
    assert "sk-EXAMPLE0" in out          # 어느 키인지 식별은 가능해야 조사가 된다
    assert "deadbeef0cafef00d" not in out   # 전체는 남지 않는다


def test_scrub_is_idempotent():
    once = scrub("Patient/abc GET /patient/xyz")
    assert scrub(once) == once


def test_scrub_leaves_ordinary_text_alone():
    msg = "grounded=True review=False sources=2"
    assert scrub(msg) == msg


def test_filter_rewrites_log_record():
    f = MessageScrubFilter()
    rec = logging.LogRecord("cerno", logging.INFO, __file__, 1,
                            "환자 %s 조회", ("Patient/secret-id",), None)
    f.filter(rec)
    assert "secret-id" not in rec.getMessage()


# ── 해시 ────────────────────────────────────────────────────

def test_hash_is_stable_and_irreversible():
    a, b = hash_id("12345"), hash_id("12345")
    assert a == b                      # 추이를 볼 수 있어야 한다
    assert "12345" not in a
    assert a != hash_id("12346")


# ── 지표 ────────────────────────────────────────────────────

def test_metrics_track_the_four_promises():
    metrics.reset()
    metrics.record_ask(grounded=True, needs_review=False, degraded=False, latency_s=1.0)
    metrics.record_ask(grounded=False, needs_review=True, degraded=True, latency_s=3.0,
                       refused=True)
    metrics.record_busy()

    s = metrics.snapshot()
    assert s["counters"]["ask_total"] == 2
    assert s["ratios"]["grounded"] == 0.5
    assert s["ratios"]["needs_review"] == 0.5
    assert s["ratios"]["degraded"] == 0.5
    assert s["ratios"]["model_refused"] == 0.5
    # 증설 판단의 1차 지표
    assert s["counters"]["serving_busy"] == 1
    metrics.reset()


def test_metrics_snapshot_is_empty_safe():
    metrics.reset()
    s = metrics.snapshot()
    assert s["ratios"]["grounded"] is None
    assert s["latency_s"]["p50"] is None


def test_metrics_carry_no_identifiers():
    """지표에 의료진·환자 식별자가 섞이면 관측이 곧 유출 경로가 된다."""
    metrics.reset()
    metrics.record_ask(grounded=True, needs_review=False, degraded=False, latency_s=1.0)
    blob = str(metrics.snapshot())
    assert "Patient" not in blob and "clinician" not in blob
    metrics.reset()


# ── 큐 대기 (AI 서버 v2.124.66) ─────────────────────────────

def test_queue_wait_is_more_sensitive_than_busy_counter():
    """단일 키 직렬화는 429 를 내지 않고 조용히 느려진다 — serving_busy 로는 안 잡힌다."""
    metrics.reset()
    for ms in (0, 591, 1091, 1328):
        metrics.record_queue(ms, None, 2)
    q = metrics.snapshot()["queue_wait_ms"]
    assert q["waited_ratio"] == 0.75       # 4건 중 3건이 대기
    assert q["max"] == 1328.0
    assert metrics.snapshot()["counters"].get("serving_busy", 0) == 0
    metrics.reset()


def test_zero_wait_is_distinguished_from_no_header():
    """서버가 0 도 명시하는 이유 — '큐를 안 탐' 과 '대기 없음' 은 다르다."""
    metrics.reset()
    assert metrics.snapshot()["queue_wait_ms"]["waited_ratio"] is None   # 표본 없음
    metrics.record_queue(0, None, 1)
    assert metrics.snapshot()["queue_wait_ms"]["waited_ratio"] == 0.0    # 대기 없음
    metrics.reset()


@pytest.mark.asyncio
async def test_client_records_queue_headers():
    import httpx
    from cerno.clients.ai import AiClient, KeyResolver

    metrics.reset()

    def h(req):
        return httpx.Response(200, json={"answer": "ok"}, headers={
            "X-Queue-Waited-Ms": "591", "X-Queue-Position": "1", "X-Queue-Active": "2"})

    ai = AiClient(base_url="https://ai.example",
                  keys=KeyResolver(mapping={"12345": "sk-a"}),
                  transport=httpx.MockTransport(h))
    await ai.chat([{"role": "user", "content": "q"}], clinician_id="12345")

    s = metrics.snapshot()
    assert s["queue_wait_ms"]["max"] == 591.0
    assert s["counters"]["queue_position_max"] == 1
    metrics.reset()


@pytest.mark.asyncio
async def test_missing_queue_headers_are_not_recorded():
    import httpx
    from cerno.clients.ai import AiClient, KeyResolver

    metrics.reset()
    ai = AiClient(base_url="https://ai.example",
                  keys=KeyResolver(mapping={"12345": "sk-a"}),
                  transport=httpx.MockTransport(
                      lambda r: httpx.Response(200, json={"answer": "ok"})))
    await ai.chat([{"role": "user", "content": "q"}], clinician_id="12345")
    assert metrics.snapshot()["queue_wait_ms"]["samples"] == 0
    metrics.reset()


# ── 키 만료 경보 (리스크 #7 심층방어) ───────────────────────

def test_key_expiry_unset_is_none_not_crash(monkeypatch):
    from cerno.obs import key_expiry
    monkeypatch.delenv("CERNO_AI_KEY_EXPIRES", raising=False)
    assert key_expiry.status() is None


def test_key_expiry_malformed_disables_not_crashes(monkeypatch, caplog):
    """설정 오타가 기동 실패나 오경보가 되면 안 된다 — 로그 남기고 비활성."""
    from cerno.obs import key_expiry
    monkeypatch.setenv("CERNO_AI_KEY_EXPIRES", "2027/07/30")
    with caplog.at_level(logging.ERROR):
        assert key_expiry.status() is None
    assert "파싱 실패" in caplog.text


def test_key_expiry_warning_at_d7_boundary(monkeypatch):
    from datetime import date
    from cerno.obs import key_expiry
    monkeypatch.setenv("CERNO_AI_KEY_EXPIRES", "2027-07-30")
    ok = key_expiry.status(today=date(2027, 7, 22))       # D-8 — 아직 조용
    warn = key_expiry.status(today=date(2027, 7, 23))     # D-7 — 경보 시작
    assert ok["warning"] is False and ok["days_left"] == 8
    assert warn["warning"] is True and warn["days_left"] == 7


def test_key_expiry_expired_logs_error(monkeypatch, caplog):
    from datetime import date
    from cerno.obs import key_expiry
    monkeypatch.setenv("CERNO_AI_KEY_EXPIRES", "2027-07-30")
    with caplog.at_level(logging.ERROR):
        st = key_expiry.check_and_log(today=date(2027, 8, 2))
    assert st["days_left"] == -3
    assert "만료됐다" in caplog.text


def test_metrics_endpoint_carries_key_expiry(monkeypatch):
    """만료일이 /metrics 에 실려야 스크래퍼가 경보를 걸 수 있다."""
    import os
    os.environ.setdefault("CERNO_API_KEY", "test-api-key")
    from fastapi.testclient import TestClient
    from cerno.api.main import app
    monkeypatch.setenv("CERNO_AI_KEY_EXPIRES", "2027-07-30")
    body = TestClient(app).get("/metrics").json()
    assert body["ai_key"]["expires"] == "2027-07-30"
    assert set(body["ai_key"]) == {"expires", "days_left", "warning", "mapped_clinicians"}


def test_snapshot_review_ratios():
    """오도율·검수 제출율 파생 비율 (17 기획 P0-2)."""
    metrics.reset()
    for _ in range(4):
        metrics._s.counters["ask_total"] += 1
    metrics.record_review(usefulness="misleading", citation_ok=False, missing=False)
    metrics.record_review(usefulness="helpful", citation_ok=True, missing=False)
    r = metrics.snapshot()["review"]
    assert r["misleading_ratio"] == 0.5
    assert r["submit_ratio"] == 0.5          # 2/4
    metrics.reset()


def test_persist_snapshot_appends_jsonl(tmp_path):
    """재기동 유실 대비 — 줄 단위 append, 실패는 예외를 밖으로 안 낸다."""
    import json
    p = tmp_path / "metrics-daily.jsonl"
    metrics.persist_snapshot(str(p))
    metrics.persist_snapshot(str(p), reason="shutdown")
    lines = [json.loads(x) for x in p.read_text().splitlines()]
    assert [x["reason"] for x in lines] == ["daily", "shutdown"]
    assert "counters" in lines[0] and "ts" in lines[0]
    metrics.persist_snapshot(str(tmp_path / "no-dir" / "x.jsonl"))   # OSError → 로그만


def test_suggest_metrics_section():
    """추천 계측 (17 기획 P0-6) — 사용률·빈 목록 비율·지연."""
    metrics.reset()
    metrics.record_suggest(questions=5, latency_s=5.5)
    metrics.record_suggest(questions=0, latency_s=1.2)
    s = metrics.snapshot()["suggest"]
    assert s["samples"] == 2 and s["empty_ratio"] == 0.5
    assert s["p95"] >= s["p50"] >= 1.2
    metrics.reset()
