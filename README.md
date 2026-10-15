# Cerno

병원 안에 머무는 의료진별 개인화 임상 AI — 온프레미스 RAG + 계층형 지식 거버넌스.

> **v0.1.8** · 🚀 **섀도우 파일럿 진행 중** (08-04 개시 · 2주 · 08-18 회고) · 비임상 범위 · [CHANGELOG](CHANGELOG.md)
> **본 시스템은 의료기기가 아니며 진단·처방을 자동 확정하지 않는다. 최종 판단은 의료진에게 있다.**

## 상태 (2026-08-04)

| 항목 | 상태 |
|---|---|
| 파일럿 | 🚀 **섀도우 개시(08-04)** — 의사 2명(호흡기·순환기, 실 launch 확증·전용 키 1:1), 2주 후 회고([`13`](13_파일럿_운영계획.md) 체크리스트) |
| 배포 | compose 배포 — 웹만 노출 · TLS 인증서 ([`deploy/`](deploy/)) |
| 서고 | org **15건**(내과·순환기 지침 요점 — [`scripts/seed_org_docs.py`](scripts/seed_org_docs.py), ⚠️ AI 요약본·원문 교체 권장) |
| 구현 | Phase 0~4 + 질의 화면 + 대화형(이어서·기록 레일·로그 보관) + 환자 맞춤 추천(카테고리·위험도·근거·확장 방향) + **회고 계측 봉합·다듬기**([`17`](17_추가기능_기획.md) P0·P1 전건) |
| 계약 테스트 | **267** (Python) + **11** (RS256/JWKS) |
| 라이브 E2E (AI 서버) | 골든셋 **N=10 정식 기준선** 6/6 · 누출 0 · p95 2.82초 — 주간 자동 회귀([`scripts/weekly_eval.sh`](scripts/weekly_eval.sh), 월 06:00) |
| 남은 외부 항목 | 규제 범위 결정(법무) — 미결 시 비임상 한정 기본값으로 운영 |

## 문서

| 문서 | 내용 |
|---|---|
| [`00_문서검토_및_개발계획.md`](00_문서검토_및_개발계획.md) | 검토·근거·전략 판단 |
| [`01_구현계획.md`](01_구현계획.md) | **실행 계획 (정본)** |
| [`04_성능_기준선.md`](04_성능_기준선.md) | N=10 기준선 |
| [`06_동시성_실측.md`](06_동시성_실측.md) | 🔴 단일 키 직렬화 |
| [`13_파일럿_운영계획.md`](13_파일럿_운영계획.md) | 섀도우 운영·검수·중단 기준 (2명·2주 잠정) |
| [`14_질의화면_UX기획.md`](14_질의화면_UX기획.md) | 질의 화면 설계 (계약→화면 1:1) |
| [`17_추가기능_기획.md`](17_추가기능_기획.md) | **추가 기획 정본** — P0·P1 ✅ 반영, P2 회고 후, §9 문헌 조사 |
| [`docs/`](docs/) | 백서 · 연동 사양서 · HIS 요청서 · **사용안내(의료진용)** |

## 설계 요약

- **새로 짓지 않는다** — RAG·임베딩·벡터DB·근거 게이트·출처 추적·DUR은 AI 서버(WeRU.B) 자산을 호출한다.
- **신원은 HIS가 넘겨준다** — SMART on FHIR EHR launch. 검증된 `fhirUser`의 `Practitioner/{id}`가 곧 RAG 소유자 격리 키. 진입점은 차트(HIS launch) 단일 — twin 연동은 추후 신원 비중개 딥링크로만.
- **근거가 없으면 생성하지 않는다** — "근거로 확인되지 않습니다"는 오류가 아니라 약속. 안전 신호(DUR·고위험)는 근거 유무와 독립.
- **PHI를 저장하지 않는다** — FHIR 응답은 메모리에서만, 로그 마스킹, 검수 기록도 correlation_id뿐.
- **현 사양에서 완결한다** — RTX 5080 16GB · Ollama · 상주 해제(요청 시 탑재) 운영 대응(launch 예열). GPU 증설을 전제하지 않는다.

## 구성

```
cerno-web  (Next.js)   launch·세션·프록시                        ← 외부 노출
  /portal              질의(기본) | 서고 탭
  /patient/[id]        진료 중 질의 (환자 맥락 자동 결속)
cerno-api  (FastAPI)   auth·library·fhir·ask·safety·review       ← 내부 전용
     │
     ├── HIS  /fhir/R4        per-user SMART token (patient/*.read)
     └── AI서버 (WeRU.B)      RAG · 생성 · DUR · 워밍
```

## 의존 시스템

| 시스템 | 위치 | 용도 |
|---|---|---|
| Hospital RUN (HIS) | `../Hospital RUN` · `his.example.org` | FHIR R4 근거 조회, SMART 인증 |
| WeRU.B (AI 서버) | `../AI_server` · `example.org/ollama` | RAG·임베딩·생성·DUR |
| twin | `../twin` | 인증·규제 산출물 참조 구현 |

## 실행

```bash
cp .env.example .env      # 값 입력 — 비면 컨테이너가 뜨지 않는다(${VAR:?})
docker compose up -d --build
```

**노출 원칙:** `cerno-web`만 뜬다(루프백 3400, 원내 nginx 뒤). `cerno-api`와 redis는
포트를 열지 않는다 — api가 직접 노출되면 프록시의 신원 서명 계층을 우회할 수 있다.

로컬 개발 (AI 서버 없이 — 스텁 사용):

```bash
python3 scripts/dev_ai_stub.py &                      # 계약 재현 스텁 :8399
CERNO_API_KEY=devkey CERNO_AI_BASE=http://127.0.0.1:8399 CERNO_AI_KEY=sk-dev \
  CERNO_CONFIG_DIR=$PWD/config \
  python3 -m uvicorn cerno.api.main:app --app-dir services/cerno-api --port 8300 &
cd services/cerno-web && npm install && \
  CERNO_API_INTERNAL=http://127.0.0.1:8300 CERNO_API_KEY=devkey \
  CERNO_SESSION_SECRET=devsecret npm run dev
# 세션 쿠키(HIS launch 대체)는 12_키전환_런북이 아닌 로컬 전용 —
# scripts/dev_ai_stub.py 상단 주석 참조. "췌장" 포함 질의 = 근거 없음 상태 재현.
```

## 테스트·평가

```bash
python3 -m pytest -q                 # 계약 테스트 220건
python3 eval/harness.py --n 10       # 성능 기준선 (N<10 이면 정식 아님을 경고)
python3 eval/concurrency.py --n 4    # 동시성·키 버킷 실측 (12 런북 §1)
```

평가 하니스는 실패·개인자료 누출 시 종료 코드 1 — CI 게이트로 쓸 수 있다.

## 운영 관측

`/metrics`(cerno-api 내부): grounded·review 비율, 큐 대기 p95(**증설 판단 1차 지표**),
검수 오도율·제출율(**파일럿 중단 판단 입력**), suggest 지연·빈목록 비율,
`ai_key.days_left`(만료 D-7 경보)·`mapped_clinicians`, `ai_embed.degraded`(임베딩
능동 프로브 — 경보 시 1차 가설은 "AI 서버 작업 중").

**영속 계측(재기동 무손실):** 검수 기록 `review_log`(SQLite) · 지표 스냅샷
`metrics-daily.jsonl`(일 1회+shutdown) · 충실도 표본 일일 제출(AI 서버 야간 채점) ·
데이터 볼륨 백업 크론(20:30 UTC, `scripts/backup_data.sh`, 7일 롤링).
