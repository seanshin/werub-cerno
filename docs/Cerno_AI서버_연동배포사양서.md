# Cerno — 로컬 AI 서버(GPU) 연동·배포 사양서

> 수신: 원내 로컬 AI 서버(WeRU.B) 운영팀 · 발신: Cerno 파일럿 담당 · **버전: v2.1 (2026-07-30)**
> 목적: 기 구축된 온프레미스 AI 서버 위에 **Cerno(계층 RAG + 지식 거버넌스 + 개인화)** 를 얹기 위한 사양·연동 정의
> 관련: Cerno 백서, HIS(EMR) 연동 요청서, `01_구현계획.md`, `02_Phase0_실측결과.md`

> **v2.0 개정 요지** — 실측(2026-07-30) 및 설계 결정 반영.
> ① GPU 사양을 **RTX 5080 16GB**로 확정(v1.0의 "24GB+ 권장" 정정) ② **상주 해제·on-demand** 전환
> ③ 자체 서빙 런타임(`serve.py`) **폐기** ④ **LoRA 어댑터 파킹**(현 스택 불가)
> ⑤ 인증을 **HIS EHR launch(per-user)** 로 확정 ⑥ 성능 목표를 **warm/cold 분리**로 기술
>
> **v2.1 개정 (AI 서버 회신 반영)** — ⓐ 서빙 경로를 heavy 레인 → **`/api/chat`** 으로 정정(A-3)
> ⓑ **의료진별 API 키** 채택 — 공정 큐 버킷이 API 키라 단일 키는 동시 1건(A-1)
> ⓒ 충실도 검증을 **비동기 사후 샘플링**으로(A-2 — 실시간 API 미노출)
> ⓓ 컬렉션 3개 고정으로 백서와 통일(A-4) ⓔ `num_ctx` 는 서버가 강제(C-1)

---

## 1. 개요

의료진 소수(1~10명) 대상, 각 의료진의 지식·설명 방식을 반영한 개인화 AI를 **원내 GPU 서버**에 배포한다.

**핵심 원칙 — 새로 짓지 않는다.** AI 서버(WeRU.B)에 이미 RAG·임베딩·벡터DB·근거 게이트·출처 추적·
DUR 정본·충실도 검증이 구현되어 운영 중이다. Cerno는 이를 **호출**하며, 고유 책임은 셋으로 한정한다.

1. **의료진 신원 결속과 계층 접근통제**
2. **Cerno Library** — 매니페스트·거버넌스·인제스션·관리 포털
3. **HIS FHIR 근거 조회**

**개발·연동 원칙.** Cerno는 EMR에 융합하지 않고 **별도로 독립 개발·배포되는 시스템**이며, EMR과는
표준 **FHIR** 로만 느슨하게 연동한다. 규제 범위 분리, AI의 빠른 개선 주기, GPU 인프라 분리,
PHI 비저장 경계 유지를 위해서다.

---

## 2. 시스템 구성

```
              [HIS 진료화면] ──EHR launch(?iss=&launch=)──┐
                                                          ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  Cerno-web (Next.js) — launch·세션·프록시·포털   [외부 노출]  │
   └───────────────────────┬─────────────────────────────────────┘
                           │ X-Cerno-User/-Role/-Sig/-Exp (HMAC·exp 60s)
   ┌───────────────────────▼─────────────────────────────────────┐
   │  Cerno-api (FastAPI) — auth·library·fhir·ask·safety [내부]   │
   └──────┬─────────────────────────────────┬────────────────────┘
          │ FHIR read (의료진 본인 권한)      │ RAG·생성·안전
          ▼                                 ▼
   [HIS /fhir/R4]                    [AI 서버 (WeRU.B) · GPU]
    per-user SMART token              /api/rag/index|search|ask
    patient/*.read                    /api/models/warm
                                      /api/chat  (heavy 레인 아님)
                                      /api/medical/dur/check
```

---

## 3. 리소스 (실측 확정)

| 항목 | 실측/확정 | 비고 |
|------|-----------|------|
| GPU | **NVIDIA RTX 5080 · 16,303 MiB** | v1.0의 "24GB+ 1장 권장"을 실사양으로 정정 |
| 서빙 형태 | **상주 해제 · on-demand** (AI 서버 v2.124.61 적용 완료) | `llm_resident` config · `GET\|POST /api/models/residency` |
| 실효 런타임 | `KEEP_ALIVE=5m` · **`MAX_LOADED_MODELS=2`** · `NUM_PARALLEL=2` · `CONTEXT_LENGTH=8192` · `KV_CACHE_TYPE=q8_0` | ⚠️ `EnvironmentFile`이 systemd drop-in을 덮어씀 — `systemctl show`를 믿지 말 것 |
| 저장소 | 기존 AI 서버 자원 | 가중치·벡터DB 모두 기존 |
| 네트워크 | 내부망 | 외부 인터넷 불필요 |

**VRAM 예산 (업무시간 실질)**

⚠️ **상주 해제 ≠ 기본 모델이 VRAM에서 사라짐.** appServer가 분당 ~16회 호출하므로
업무시간에는 `qwen2.5:14b`가 **사실상 항상 웜**이다.

| 구성 | VRAM |
|---|---|
| `qwen2.5:14b` (기본 채팅 · Cerno 공유) | 10.0~10.5GB |
| `bge-m3` 임베딩 | **1.16GB** (`num_ctx=2048` 명시 시) / 미명시 **4.5GB** |
| 실시간 STT medium (VoiceEMR) | 1.96GB |
| **합계** | **13.6GB / 16.3GB** — 여유 2.7GB |

**Cerno 전용 베이스는 자리가 없다.** med42-8b(8.5GB)·qwen2.5:7b(5GB) 모두 초과하며,
`MAX_LOADED_MODELS=2` 슬롯도 14b + bge-m3로 이미 찬다. → **기본 채팅 모델을 공유한다**(§4).

배치 STT large-v3(3.84GB)는 외래 피크와 시간 분리한다.

---

## 4. 모델 서빙

- **베이스 모델(확정): `qwen2.5:14b` — 기본 채팅 모델 공유.** Cerno 전용 베이스를 띄우지 않는다(§3 예산).
  이점: 추가 VRAM **0** · 업무시간 **상시 웜**(콜드 사실상 없음) · KorMedMCQA **67.5%**(7b 60.5% 우위) ·
  규제 가드 `medical_pin_public_models`의 `medical_public_substitute` 기본값과 **동일 모델**.
  - **증설 시 A/B 후보(전량 설치 완료 · 리드타임 0):** `thewindmom/llama3-med42-8b`(8.5GB·의료텍스트
    1순위 98.7·Llama3 CL) · `MedAIBase/MedGemma1.5:4b`(7.8GB·2순위·HAI-DEF) · `qwen2.5:7b` ·
    `koesn/llama3-openbiollm-8b` · `meditron` · `exaone3.5:7.8b`. 채택 시 **상용 라이선스 확인 필수**.
- **임베딩 `num_ctx`:** AI 서버 `client/rag.py`의 `EMBED_NUM_CTX`(기본 2048)를 **서버가 강제**하므로
  `/api/rag/*` 경유 시 호출자 부담이 없다. Ollama `/api/embed`를 **직접** 부를 때만 명시가 필요하다
  (미명시 시 bge-m3가 1.16GB가 아니라 4.5GB를 잡는다).
- **런타임: `POST /api/chat` + `POST /api/models/warm`** (v2.1 정정). ~~스캐폴드 FastAPI(`serve.py`)~~ **폐기**.
  ~~heavy 레인~~ 도 쓰지 않는다 — 현재 `heavy_enabled=false`이고 `heavy_model`이 `qwen3.6:35b-a3b`(35B)라
  켜는 순간 상주 모델을 축출해 §3 예산이 깨진다. single-flight이 필요한 이유(대형 모델 동시 적재 OOM)가
  14b 공유에는 없고, 동시성 보호는 공정 큐가 이미 한다(§6).
- **추론 파라미터 고정:** `temperature=0.2`, `seed=42`, 토큰 상한.
  ⚠️ **결정성의 수단은 seed이지 greedy가 아니다** — `temperature=0`은 한국어 붕괴·JSON 미닫힘을 유발한 전례가 있다.
- **~~의료진 LoRA 핫스왑~~ → 파킹.** 현 스택(Ollama)은 어댑터를 모델에 구워 배포하므로 의료진 전환이
  **전체 모델 콜드 스왑**이 된다. 슬롯이 2개뿐이라(14b + bge-m3 로 이미 소진) 다수 의료진
  동시 운용이 불가하다.
  **병목은 GPU 크기가 아니라 서빙 런타임이며, 해소하려면 vLLM 전환이 필요하다**(별도 트랙).
  현 범위의 개인화는 §9를 따른다.

---

## 5. 지식(Cerno Library)

**AI 서버 기존 RAG 자산을 그대로 사용한다.** 신규 구축 없음.

| 요소 | AI 서버 현황 |
|---|---|
| 임베딩 | **BGE-m3**(`client/rag.py`) · 온프레미스 · 664MB · LLM과 공존 |
| 벡터DB | ChromaDB + **FTS5 하이브리드(가중 RRF)** + rerank |
| 근거 게이트 | `_drop_weak_matches` · 코사인 `confidence` · `medical_reference_min_similarity`(기본 0.45) |
| 출처 | `source`·`version` 노출 · `routes/provenance.py` 레코드 단위 이력 |
| 충실도 | `core/faithfulness.py` — ⚠️ **야간 배치 전용·API 미노출**(§8) |

**계층 컬렉션 — 3컬렉션 + owner 메타.** `/api/rag/search`가 단일 컬렉션만 받으므로 의료진별로 쪼개지
않는다. 공개범위별 `cerno_priv`·`cerno_grp`·`cerno_org`로 **물리 분리**하고 개인 구분은 메타데이터로 한다.
검색 호출은 인원과 무관하게 **항상 3회**(병렬·RRF 병합) → 의료진별 키 기준 **의료진당 분당 40질의**.
⚠️ 컬렉션 생성 시 **`hnsw:space=cosine` 확인 필요** — L2 기본값으로 생성되면 유사도 점수가 압축되어
권위 역전이 일어난 사고 전례가 있다(AI 서버 v2.124.8).

**인제스션:** 매니페스트 → 거버넌스 검증(PHI·출처·저작권) → `/api/rag/index` **배치**(≤500·upsert 멱등).
단건 1.47s/건 vs 배치 ~12ms/건이므로 배치가 필수다.

**근거 부족 시 생성하지 않고 회피.** 답변에 **공개범위·출처 표기**.

---

## 6. 제공 API

내부망 전용. **인증은 §7의 HIS EHR launch 세션을 따른다.**

**POST /ask**
```json
{ "query": "…", "specialty": "cardiology",
  "patient_context": { "source": "emr", "patient_id": "<FHIR Patient.id>" } }
```

> ⚠️ **`clinician_id`는 요청 본문에 없다.** 검증된 SMART `id_token`의 `fhirUser`
> (`Practitioner/{id}`)에서만 도출한다. 본문에 실려 오고 클레임과 다르면 **403**.
> `patient_context`는 **FHIR `Patient.id`** 이며 MRN이 아니다.

응답: `answer`, `sources[{citation, visibility, confidence}]`, `grounded`,
`needs_human_review`, `review_reasons`, `meta{prompt_version, model, warm}`.

**GET /health** — 상태·적재 확인.

---

## 7. HIS 연동 (FHIR · per-user)

- **인증(확정): SMART on FHIR EHR Launch.** HIS가 `?iss=&launch=`로 Cerno를 호출하며 신원을 넘긴다.
  Cerno는 **의료진 본인 권한의 per-user 토큰**으로 HIS를 읽는다.
  → HIS 요청서 §6의 "AI 요청도 해당 의료진 권한·스코프 범위 내로 제한"이 자동 충족된다.
  ~~SMART Backend Services(M2M 서비스계정)~~ 는 광범위 계정을 쥐게 되어 **채택하지 않는다**.
- **대상 리소스(8종·읽기 전용):** `Patient`·`Condition`·`Observation`·`DiagnosticReport`·**`Procedure`**·
  `MedicationRequest`·`AllergyIntolerance`·`DocumentReference`.
- **스코프:** `patient/*.read` 9종. **전부 HIS `scopes_supported`에 이미 선언되어 있다.**
- **PHI 비저장:** FHIR 조회 데이터는 메모리에서만 사용, **디스크·로그 원문 저장 금지**.
  영속 저장은 매니페스트·설정·감사로그(비PHI)와 **per-user 토큰(TTL·인증된 저장소)** 뿐이다.
- **degrade:** 조회 실패 → "근거 조회 불가". 토큰 만료 → SMART 재진입 요구.

---

## 8. 안전·가드레일 (필수)

근거 부족 시 회피 + 사람 검토 플래그, **출처 표기 의무화**, 고위험 신호(용량·응급·금기·소아/임신)
`needs_human_review=true`, 진단·처방 자동 확정 금지(human-in-the-loop).

- **DUR:** AI 서버 `/api/medical/dur/check` **식약처 정본 9/9** 연계.
- **충실도:** ⚠️ `core/faithfulness.py` 는 **야간 배치 전용이며 요청별 API 가 없다.**
  실시간 검증은 질의당 LLM 호출 2회(주장 분해 + 판정)가 추가되어 p95 목표·동시 슬롯과 충돌한다.
  → 파일럿은 **근거 게이트 + 출처 표기 + `needs_human_review`** 로 갈음하고, 충실도는
  **비동기 사후 샘플링**(하루치 답변 중 N건을 AI 서버 야간 배치에 태움)으로 확보한다.

---

## 9. 개인화 (현 사양 · 무어댑터)

LoRA 파킹(§4)에 따라 개인화는 어댑터 없이 구현한다.

| 수단 | 구현 |
|---|---|
| 설명 방식·강조점·서술 순서 | 의료진별 시스템 프롬프트 + **버전 레지스트리**(`cerno.narrative@1.0.0`, PATCH/MINOR/MAJOR ↔ 경미/영향평가/중요) |
| 판단 흐름 | few-shot — 개인 서고에서 본인의 과거 답변·노트 검색 주입 |
| 관점 반영 | `cerno_priv` 검색 가중치 |

**효용은 측정한다** — Phase 3 하니스의 개인화 적합도 지표가 LoRA 투자 판단의 근거가 된다.

> ~~증류(`distill_generate.py`)~~ **파킹.** LoRA 학습용이므로 서빙이 불가한 현 스택에서는 무의미하다.
> 재개 시 법무 게이트(`DISTILL_LICENSE_APPROVED=1`)·PHI 미포함·**의료진 본인 동의**가 선행한다.

---

## 10. 보안·컴플라이언스

- 전 구성요소 **온프레미스**, 외부 API 호출 없음.
- **신원 위조 방지(필수):** iss 화이트리스트 · `id_token` 서명·iss·aud·exp 검증(fail-closed) ·
  `fhirUser` 엄격 파싱(폴백 금지) · 세션 HMAC 재검증 · 신원 헤더 서명(exp 60s) · 경로 traversal 이중 가드.
- **per-user 토큰 저장소는 인증 필수**(`requirepass`) — 미설정 시 기동 실패시킬 것.
- 시크릿은 환경변수(하드코딩 금지), 로그 **PHI 마스킹**.
- 개인정보보호법·의료법·원내 정보보안/IRB. **파일럿은 비임상 범위로 한정**(규제 범위 결정 전).

---

## 11. 관측성·운영

- 로깅: 상관관계 ID, 의료진 id(해시), 지연, grounded/review·도메인 태그(PHI 마스킹).
- 모니터링: GPU·메모리, 지연, 오류율, 근거미달·고위험 비율, **single-flight 409 발생률**.

**성능 목표 (실측 기반 · warm/cold 분리)**

| 지표 | 목표 | 실측 근거 |
|---|---|---|
| p95 (warm · RAG 포함) | **< 5초** | warm 사이클 **0.83초** (embed 0.15 + generate 0.68) |
| 콜드 최초 1회 | **≤ 10초** | 모델 콜드 로드 **2.9초** |
| 동시 진료실 | 파일럿 실측 후 확정 | single-flight 상한 = 증설 판단 1차 지표 |

> 단일 p95 수치로 쓰면 조건이 숨는다. 베이스를 med42-8b로 바꾸면 콜드가 늘어난다.
> ⚠️ 위 수치는 단발 관측이다. **정식 기준선은 N≥10 · 워밍업 1회 후** 재산출한다.

---

## 12. 컴포넌트 매핑 (실물 기준)

| 기능 | 소재 | 비고 |
|------|------|------|
| RAG 색인·검색·질의 | **AI 서버** `routes/rag.py` | 재사용 |
| 임베딩(BGE-m3) | **AI 서버** `client/rag.py` | 재사용 |
| 근거 게이트 | **AI 서버** `_drop_weak_matches` | 재사용 |
| 출처 추적 | **AI 서버** `routes/provenance.py` | 재사용 |
| 충실도 검증 | **AI 서버** 야간 배치 | ⚠️ API 미노출 — **비동기 사후 샘플링**으로 사용(§8) |
| DUR 정본 | **AI 서버** `/api/medical/dur/check` | 재사용 |
| 생성·워밍 | **AI 서버** `POST /api/chat`·`POST /api/models/warm` | heavy 레인 아님(§4) |
| 신원 결속·접근통제 | **Cerno** `cerno-web/lib/smart.ts`·`cerno-api/auth/` | 신규(twin 이식) |
| 매니페스트·거버넌스·인제스션 | **Cerno** `cerno-api/library/` | 신규 |
| HIS FHIR 커넥터 | **Cerno** `cerno-api/fhir/` | 신규 |
| 질의 오케스트레이션 | **Cerno** `cerno-api/ask/` | 신규 |
| 서빙·개인화 어댑터 | **Cerno** `cerno-api/serving/`·`personal/` | 신규(증설 대비 격리) |
| 관리 포털 | **Cerno** `cerno-web/app/portal/` | 신규 |

---

## 13. AI 서버팀 회신 요청 체크리스트

**실측으로 확정된 항목** (2026-07-30 · 회신 불요)

- [x] GPU/VRAM — RTX 5080 16,303 MiB · RAM 31.2GB · 디스크 3.1GB/s
- [x] 실효 런타임 — **`MAX_LOADED_MODELS=2`** · `NUM_PARALLEL=2` · `KEEP_ALIVE=5m` · `KV_CACHE_TYPE=q8_0`
- [x] 벡터DB·임베딩 — ChromaDB + FTS5, BGE-m3(1.16GB @ `num_ctx=2048`)
- [x] 베이스 모델 — **`qwen2.5:14b` 공유 확정**. 증설용 후보 8종 설치 완료
- [x] 서빙 방식 — on-demand (`llm_resident`)
- [x] **상주 해제** — AI 서버 v2.124.61 적용 완료(엔진 `keep_alive=-1` 교정 포함)
- [x] 콜드/웜 비용 — 콜드 TTFT 2.5~3.9초 · 웜 0.13초

**협의 필요**

- [ ] **Cerno API 키 발급** — ⚠️ 만료 D-7 경보 등록 필수(2026-07 실제 3일 단절 이력)
- [ ] `cerno_priv`·`cerno_grp`·`cerno_org` 컬렉션 생성 및 rate limit 정책 확인
- [ ] 동시성 목표 — single-flight 상한과 진료 수요 대조
- [ ] 담당자 및 일정
