"""Library API 계약 테스트 (Phase 2-4 백엔드).

핵심: 신원 없이는 아무것도 안 된다 · owner 는 신원이 진실이다 · 목록 격리가 검색과
같아야 한다 · 부분 적재를 성공으로 보고하지 않는다.
"""
import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

KEY = "test-api-key"
os.environ.setdefault("CERNO_API_KEY", KEY)
os.environ.setdefault("CERNO_AI_BASE", "https://ai.example")
os.environ.setdefault("CERNO_AI_KEY", "sk-test")

from fastapi.testclient import TestClient          # noqa: E402

from cerno.api import deps                          # noqa: E402
from cerno.api.main import app                      # noqa: E402
from cerno.clients.ai import AiClient, KeyResolver  # noqa: E402
from cerno.library.store import DocumentStore       # noqa: E402

ME, OTHER = "12345", "67890"


def headers(uid=ME, role="clinician"):
    exp = int(time.time()) + 60
    sig = hmac.new(KEY.encode(), f"{uid}.{role}.{exp}".encode(), hashlib.sha256).hexdigest()
    return {"X-Cerno-User": uid, "X-Cerno-Role": role,
            "X-Cerno-Sig": sig, "X-Cerno-Exp": str(exp)}


def manifest(**kw):
    base = {"doc_id": "doc-1", "title": "제목", "visibility": "private", "phi_reviewed": True}
    base.update(kw)
    return base


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = DocumentStore(str(tmp_path / "t.db"))
    monkeypatch.setattr(deps, "get_store", lambda: store)
    monkeypatch.setattr(deps.get_store, "__wrapped__", lambda: store, raising=False)

    def ok(req):
        return httpx.Response(200, json={"count": 1, "results": []})

    ai = AiClient(base_url="https://ai.example",
                  keys=KeyResolver(mapping={ME: "sk-a", OTHER: "sk-b"}),
                  transport=httpx.MockTransport(ok))
    monkeypatch.setattr(deps, "get_ai", lambda: ai)
    monkeypatch.setattr("cerno.api.library.get_store", lambda: store)
    monkeypatch.setattr("cerno.api.library.get_ai", lambda: ai)
    monkeypatch.setattr("cerno.api.library.groups_of", lambda i: ["cardio-dept"])
    return TestClient(app), store


# ── 신원 게이트 ─────────────────────────────────────────────

def test_health_is_open_and_carries_no_identifiers(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert "circuits" in r.json()


def test_no_identity_is_401(client):
    c, _ = client
    assert c.get("/library/documents").status_code == 401


def test_forged_identity_is_401(client):
    c, _ = client
    h = headers()
    h["X-Cerno-Sig"] = "deadbeef"
    assert c.get("/library/documents", headers=h).status_code == 401


# ── 검증과 적재의 분리 ──────────────────────────────────────

def test_validate_reports_all_violations_without_indexing(client):
    c, _ = client
    r = c.post("/library/validate", headers=headers(),
               json={"manifest": manifest(phi_reviewed=False, visibility="org"), "text": "본문"})
    body = r.json()
    assert body["ok"] is False
    assert len(body["violations"]) >= 3          # 전량 보고
    assert body["collection"] == "cerno_org"


def test_validate_previews_chunk_count(client):
    c, _ = client
    r = c.post("/library/validate", headers=headers(),
               json={"manifest": manifest(), "text": "짧은 본문"})
    assert r.json()["chunk_preview"] == 1


# ── owner 는 신원이 진실이다 ────────────────────────────────

def test_owner_is_overwritten_by_verified_identity(client):
    """폼이 남의 id 를 보내도 신원으로 덮어쓴다 — 비교만 하면 위조 여지가 남는다."""
    c, store = client
    r = c.post("/library/documents", headers=headers(),
               json={"manifest": manifest(owner_clinician_id=OTHER), "text": "본문"})
    assert r.status_code == 200
    rec, _ = store.get("doc-1")
    assert rec.owner == ME


def test_governance_failure_is_422_with_violations(client):
    c, store = client
    r = c.post("/library/documents", headers=headers(),
               json={"manifest": manifest(phi_reviewed=False), "text": "본문"})
    assert r.status_code == 422
    assert r.json()["detail"]["violations"]
    assert store.get("doc-1") is None, "거부됐는데 등록부에 남았다"


# ── 목록 격리는 검색과 같아야 한다 ──────────────────────────

def test_list_excludes_other_peoples_private_docs(client):
    c, store = client
    c.post("/library/documents", headers=headers(ME),
           json={"manifest": manifest(doc_id="mine"), "text": "본문"})
    c.post("/library/documents", headers=headers(OTHER),
           json={"manifest": manifest(doc_id="theirs"), "text": "본문"})

    ids = {d["doc_id"] for d in c.get("/library/documents", headers=headers(ME)).json()["documents"]}
    assert ids == {"mine"}, "타인의 private 문서가 포털 목록에 보인다"


def test_list_includes_org_docs_from_anyone(client):
    c, _ = client
    c.post("/library/documents", headers=headers(OTHER), json={
        "manifest": manifest(doc_id="pub", visibility="org", license_approved=True,
                             source={"doi": "10.1/x"}),
        "text": "본문"})
    ids = {d["doc_id"] for d in c.get("/library/documents", headers=headers(ME)).json()["documents"]}
    assert "pub" in ids


def test_list_marks_ownership(client):
    c, _ = client
    c.post("/library/documents", headers=headers(ME),
           json={"manifest": manifest(doc_id="mine"), "text": "본문"})
    docs = c.get("/library/documents", headers=headers(ME)).json()["documents"]
    assert docs[0]["mine"] is True


# ── 삭제 ────────────────────────────────────────────────────

def test_only_owner_can_delete(client):
    c, _ = client
    c.post("/library/documents", headers=headers(ME),
           json={"manifest": manifest(doc_id="mine"), "text": "본문"})
    assert c.delete("/library/documents/mine", headers=headers(OTHER)).status_code == 403


def test_delete_is_idempotent_for_missing_doc(client):
    c, _ = client
    r = c.delete("/library/documents/nope", headers=headers())
    assert r.status_code == 200 and r.json()["deleted"] == 0


def test_delete_removes_from_registry(client):
    c, store = client
    c.post("/library/documents", headers=headers(ME),
           json={"manifest": manifest(doc_id="mine"), "text": "본문"})
    c.delete("/library/documents/mine", headers=headers(ME))
    assert store.get("mine") is None


# ── 부분 적재를 성공으로 보고하지 않는다 ────────────────────

def test_partial_ingest_is_502_not_200(tmp_path, monkeypatch):
    store = DocumentStore(str(tmp_path / "p.db"))
    calls = {"n": 0}

    def flaky(req):
        calls["n"] += 1
        return httpx.Response(500) if calls["n"] > 1 else httpx.Response(200, json={"count": 1})

    ai = AiClient(base_url="https://ai.example", keys=KeyResolver(mapping={ME: "sk-a"}),
                  transport=httpx.MockTransport(flaky))
    monkeypatch.setattr("cerno.api.library.get_store", lambda: store)
    monkeypatch.setattr("cerno.api.library.get_ai", lambda: ai)
    monkeypatch.setattr("cerno.api.library.groups_of", lambda i: [])
    c = TestClient(app)

    text = "\n\n".join(f"문단 {i}. " + "가" * 400 for i in range(700))
    r = c.post("/library/documents", headers=headers(ME),
               json={"manifest": manifest(), "text": text})
    assert r.status_code == 502
    assert r.json()["detail"]["partial"] is True
    assert store.get("doc-1") is None, "부분 적재인데 등록부에 성공으로 남았다"
