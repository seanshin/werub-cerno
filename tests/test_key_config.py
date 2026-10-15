"""CERNO_AI_KEYS 기동 검증 (17 기획 P0-4) — D-0 사고 재발 방지.

핵심: 비JSON 키맵으로는 **뜨지 않는다.** 빈 맵 강하·경고 침묵이 게이트 3(전용 키
분리)을 무증상으로 무효화한 실사고(2026-08-04)의 봉합.
"""
import hashlib  # noqa: F401  (환경 초기화 순서 고정용 — 아래 os.environ 설정 참조)
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "cerno-api"))

os.environ.setdefault("CERNO_API_KEY", "test-api-key")
os.environ.setdefault("CERNO_AI_BASE", "https://ai.example")
os.environ.setdefault("CERNO_AI_KEY", "sk-test")

from cerno.api.main import startup_key_check   # noqa: E402
from cerno.clients.ai import KeyResolver, load_key_map  # noqa: E402

D0_BROKEN = "{00000000-0000-4000-a000-000000a00003:sk-abc}"   # 실사고의 실제 형태


def test_valid_map_parses():
    assert load_key_map('{"doc-1":"sk-a","doc-2":"sk-b"}') == {
        "doc-1": "sk-a", "doc-2": "sk-b"}


def test_empty_is_allowed():
    assert load_key_map("") == {}          # 키맵 미사용 구성(공유 키 단독)은 유효


@pytest.mark.parametrize("raw", [D0_BROKEN, "[1,2]", '{"a": 1}', '{"a": ""}', "sk-abc"])
def test_broken_map_refuses(raw):
    with pytest.raises(RuntimeError):
        load_key_map(raw)


def test_resolver_env_path_refuses_broken(monkeypatch):
    monkeypatch.setenv("CERNO_AI_KEYS", D0_BROKEN)
    with pytest.raises(RuntimeError):
        KeyResolver()                      # 빈 맵 강하 금지 — 요청 시점에도 죽는다


def test_startup_check_dies_on_broken_env(monkeypatch):
    monkeypatch.setenv("CERNO_AI_KEYS", D0_BROKEN)
    with pytest.raises(RuntimeError):
        startup_key_check()


def test_startup_check_counts_and_warns_unmapped(monkeypatch, caplog):
    monkeypatch.setenv("CERNO_AI_KEYS", '{"registered-doc":"sk-a"}')
    monkeypatch.setattr("cerno.api.main.clinician_ids",
                        lambda: ["registered-doc", "keyless-doc"])
    with caplog.at_level("WARNING", logger="cerno.api"):
        assert startup_key_check() == 1
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "전용 키 없는" in joined
    assert "keyless-doc" not in joined     # 원문 id 는 로그에 안 나간다(해시만)
