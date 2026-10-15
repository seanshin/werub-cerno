"""로컬 UI 스모크용 AI 서버 스텁 — 실 AI 서버 계약의 최소 재현. **개발 전용.**

응답 형태는 cerno/clients/ai.py 의 라이브 실측 주석(2026-07-30)을 따른다:
  /api/rag/search mode=vector → results[].score 가 코사인, confidence 없음
  /api/chat → {"answer": ...}  (Ollama 형식 아님)
질의에 "췌장" 이 들어가면 전 컬렉션 0건 → ungrounded 상태 재현.

로컬 세션 쿠키 (HIS launch 대체 — CERNO_SESSION_SECRET=devsecret 기준):

    python3 - <<'PY'
    import hmac, hashlib, time
    uid, role, sec = "doc-1001", "clinician", "devsecret"
    exp = int(time.time()) + 8*3600
    sig = hmac.new(sec.encode(), f"sess:{uid}.{role}.{exp}".encode(),
                   hashlib.sha256).hexdigest()
    print(f"{uid}.{role}.{exp}.{sig}")
    PY

브라우저 콘솔에서 `document.cookie = "cerno_session=<위 값>"` 후 새로고침.
실서비스 세션은 오직 SMART launch 로만 발급된다 — 이 우회는 dev 시크릿 전제다.
"""
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

HITS = {
    "cerno_org": [{
        "id": "guide-hf__chunk_0",
        "text": "베타차단제는 안정 시 2주 간격으로 단계적으로 증량한다.",
        "score": 0.71,
        "metadata": {"citation": "대한심장학회 심부전 진료지침, 2025"},
    }],
    "cerno_priv": [{
        "id": "note-1__chunk_0",
        "text": "증량 전 심박수·혈압 확인. 악화 시 보류하고 재평가.",
        "score": 0.66,
        "metadata": {},
    }],
    "cerno_grp": [],
}


@app.post("/api/rag/search")
async def search(req: Request):
    body = await req.json()
    if "췌장" in body.get("query", ""):
        return {"count": 0, "results": []}
    return {"results": HITS.get(body.get("collection", ""), [])}


@app.post("/api/chat")
async def chat(req: Request):
    await req.json()
    return {"answer": ("안정 시 2주 간격의 단계적 증량이 권고됩니다 [1]. "
                       "목표 용량 도달 전이라도 증상 악화 시 증량을 보류하고 "
                       "재평가합니다 [2].")}


@app.post("/api/models/warm")
async def warm():
    return {"ok": True}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8399, log_level="warning")
