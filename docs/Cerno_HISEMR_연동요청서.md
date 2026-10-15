# Cerno — HIS(EMR) FHIR 연동 사양·요청서

> 수신: HIS(Hospital RUN) 운영팀 · 발신: Cerno 파일럿 담당 · **버전: v3.0 (2026-07-30)**
> 목적: **별도 독립 시스템으로 개발되는 Cerno**를 원내 **FHIR**로 연동하기 위한 데이터·인증·보안 사양
> 관련: Cerno 백서, AI 서버 연동·배포 사양서 v2.0, `01_구현계획.md`

> **v3.0 개정 요지 — 문서 성격 전환.**
> HIS가 자체 개발 시스템(Hospital RUN)이므로 v2.0의 "회신 요청" 항목 대부분을 **코드 실측으로 확정**했다(§10).
> 아울러 인증을 **SMART Backend Services(M2M) → EHR Launch(per-user)** 로 변경했다(§6).
> 남은 요청은 **엔드포인트 가드 3종 교체**와 **클라이언트 발급** 둘뿐이다.

> **⚠️ 2026-07-30 진행 상황 (본 사양서는 배경 문서 · 실행 상태는 아래 문서가 정본).**
> - §10-A 가드 교체·§10-B 클라이언트 발급 → **완료·배포** (HIS 회신, 커밋 `2042643e`)
> - launch 검증 중 결함 2건(`id_token` HS256→**RS256/JWKS 전환**, standalone `fhirUser`) → **HIS 봉합 완료**, Cerno RS256 검증 반영
> - 상세 요청 · 회신 기록은 공개판에 싣지 않는다.

---

## 1. 개요 및 연동 원칙

Cerno는 의료진 소수(1~10명) 대상 **개인화 AI 보조 도구**로, 원내 로컬 AI 서버(GPU)에서 운영된다.
환자 데이터는 원내를 벗어나지 않는다.

**연동 원칙 — 별도 개발 + 느슨한 결합.** Cerno는 EMR에 융합하지 않고 **독립적으로 개발·배포되는
별도 시스템**이며, EMR과는 **표준 FHIR API로만 연동**한다. 규제 범위 분리(AI의 의료기기성 vs EMR 인증),
AI의 빠른 개선 주기와 EMR 릴리스 주기의 분리, GPU 인프라 분리, PHI 비저장 경계 유지를 위해서다.

---

## 2. 연동 아키텍처에서 HIS의 위치

```
  [의료진] ──진료화면에서 Cerno 호출──▶ [HIS · FHIR 서버]
                                            │
              (1) EHR launch: ?iss=&launch=  │  신원·환자 컨텍스트를 HIS가 넘긴다
                                            ▼
                              [Cerno (별도 독립 시스템)]
                                            │
              (2) per-user 토큰으로 FHIR read │  의료진 본인 권한
                                            ▼
                                    답변 + 근거 출처
```

HIS의 역할은 **(1) 신원·환자 컨텍스트 전달(EHR launch)** 과 **(2) 근거용 FHIR 조회 제공** 둘이다.
Cerno는 EMR 데이터를 **영구 저장하지 않고** 질의 시점에 최소 범위만 조회한다.
Cerno의 지식 문서(개인 연구·공용 논문)는 HIS가 아니라 Cerno 관리 포털에서 관리된다.

---

## 3. FHIR 데이터 조회 (읽기 전용)

| 목적 | FHIR 리소스 | 우선순위 | HIS 현황 |
|------|-------------|----------|----------|
| 환자 기본 | `Patient` | 필수 | ✅ SMART 수용 |
| 문제목록/진단 | `Condition` | 필수 | ✅ SMART 수용 |
| 검사결과 | `Observation` | 필수 | ✅ SMART 수용 |
| 영상 판독문/보고서 | `DiagnosticReport` | 필수 | ✅ SMART 수용 |
| 시술/수술 이력 | `Procedure` | 필수 | ✅ SMART 수용 |
| 약물/처방 | `MedicationRequest` | **필수** | 🔴 **가드 교체 필요**(§10-A) |
| 알레르기 | `AllergyIntolerance` | **필수** | 🔴 **가드 교체 필요**(§10-A) |
| 경과기록/노트 | `DocumentReference` | 권장 | 🔴 **가드 교체 필요**(§10-A) |

> **v2.0에서 "권장"이던 `MedicationRequest`·`AllergyIntolerance`를 "필수"로 올렸다.**
> 백서 §8이 규정한 **고위험 플래그(용량·응급·금기·소아/임신)** 의 1차 근거가 현재 투약과 알레르기이기
> 때문이다. 이 둘 없이는 안전 가드레일의 입력이 구조적으로 결손된다.

- **FHIR 버전:** R4 (4.0.1) — 확정
- **baseURL:** `https://his.example.org/fhir/R4` — 확정
- **조회 형태:** 온디맨드(질의 시점 실시간). Bulk Data 미사용.

---

## 4. 컨텍스트 전달 (개인화 키)

- **의료진 식별자:** SMART `id_token`의 `fhirUser` → `Practitioner/{id}`.
  이 값이 Cerno의 **개인 어댑터·개인 서고 매핑 키이자 RAG 소유자 격리 키**다.
  Cerno는 이 값을 서명 검증 후에만 신뢰하며, 다른 어떤 경로로도 의료진 신원을 받지 않는다.
- **환자 컨텍스트:** SMART `launch` 시 현재 환자(`patient`) 전달.

식별자는 요청 처리에만 쓰며 영구 저장하지 않는다.

---

## 5. 비식별화·PHI 처리

- FHIR 응답에서 임상 판단에 불필요한 **직접 식별자(성명·주민번호·연락처·주소 등) 최소화** 방침 협의.
- 자유텍스트(`DocumentReference`/`DiagnosticReport` 서술) 내 식별정보 마스킹 협의.
- Cerno는 조회 데이터를 **영구 저장하지 않는다** — 메모리에서만 사용, 로그 PHI 마스킹.

---

## 6. 인증·인가 — SMART on FHIR EHR Launch (확정)

**v2.0의 Backend Services(M2M 서비스계정) 방식을 철회하고 per-user EHR Launch로 확정한다.**

| | Backend Services (철회) | **EHR Launch (채택)** |
|---|---|---|
| 권한 | Cerno가 전 환자 접근 가능한 광범위 서비스계정 보유 | **의료진 본인 권한 범위 내** |
| §6 "의료진 단위 접근 격리" 요구 | 별도 구현 필요 | **자동 충족** |
| 스코프 | `system/*` — HIS 등록 필요 | `patient/*` — **이미 전량 선언됨** |

**흐름:** HIS 진료화면 → `?iss=&launch=` → Cerno `/smart/launch` → HIS `/oauth2/authorize`(PKCE S256)
→ Cerno `/smart/callback` → 토큰 교환(confidential) → `id_token` 검증 → 세션 발급.

**요청 스코프 (9종·읽기 전용).** Cerno는 write-back하지 않는다.

```
launch openid fhirUser
patient/Patient.read patient/Condition.read patient/Observation.read
patient/DiagnosticReport.read patient/Procedure.read
patient/MedicationRequest.read patient/AllergyIntolerance.read
patient/DocumentReference.read
```

**Cerno 측 보안 이행 (twin 검증 패턴 이식):** iss 화이트리스트 · `id_token` **RS256+JWKS** 서명·iss·aud·exp 검증 (2026-07-30 HIS 전환 반영·HS256→RS256)
(실패 시 fail-closed) · `fhirUser` 엄격 파싱(폴백 금지) · 세션 HMAC 재검증 · 신원 헤더 서명(exp 60s) ·
경로 traversal 이중 가드 · per-user 토큰 저장소 인증 필수.

- **포털 접근:** EHR launch 없이 접근하는 문서 관리 포털은 **Standalone Launch**로 동일 흐름을 탄다.
  **Cerno는 자체 계정 체계를 만들지 않는다** — 신원 출처를 HIS 하나로 단일화한다.
- mTLS 적용 가능 여부 협의.

---

## 7. 감사로그·추적성

- Cerno의 모든 FHIR 조회에 대해 **감사로그(누가·언제·무슨 리소스·목적)** 기록.
  HIS는 FHIR `AuditEvent` 리소스는 미구현이나 자체 `AuditLog`가 **변이 기본 감사 ON + 명시 면제**
  구조로 존재한다(`modules/audit/audit-routes.ts`). 이 체계와의 연계 방식 협의.
- 요청마다 상관관계 ID 부여로 HIS 감사로그와 매칭. 보존기간은 원내 정책 준수.

---

## 8. (선택) EMR 화면 임베드

**현 파일럿 범위 밖 — 규제 범위 결정 후 재개.** EHR launch 흐름이 이미 서 있으므로 나중에 붙이는
비용은 작다.

- 진료 화면에서 Cerno 패널 iframe 임베드 가능 여부(`frame-ancestors` 정책 포함).
- 응답 표시: **답변 + 근거 출처**를 함께 노출(참고용, 최종 판단은 의료진).
- 결과의 EMR 저장: 기본 **미저장**.

---

## 9. 인터페이스 예시

```
GET {fhirBaseUrl}/Observation?patient={patientId}&category=laboratory&_sort=-date&_count=20
Authorization: Bearer {per-user access_token}   # EHR launch 로 발급
```

---

## 10. HIS팀 요청 사항

### 🔴 A. 엔드포인트 가드 교체 (유일한 코드 변경 · 소규모)

`MedicationRequest`·`AllergyIntolerance`·`DocumentReference` **6개 엔드포인트**가 현재
`@UseGuards(JwtAuthGuard)` 단독이라 **SMART 토큰이 거부된다**. 아래로 교체 요청.

| 파일 | 라인 | 변경 |
|---|---|---|
| `apps/api/src/modules/fhir/fhir.controller.ts` | 424·466 (`MedicationRequest`) | `JwtAuthGuard` → `FhirDualAuthGuard` + `@FhirScope('MedicationRequest','read')` |
| 〃 | 629·658 (`AllergyIntolerance`) | 〃 (`AllergyIntolerance`) |
| 〃 | 1478·1530 (`DocumentReference`) | 〃 (`DocumentReference`) |

- **`scopes_supported` 수정은 불필요하다** — `patient/MedicationRequest.read`(:145),
  `patient/AllergyIntolerance.read`(:148), `patient/DocumentReference.read`(:156)가 이미 선언되어 있다.
- 참조 구현: 같은 파일의 `Patient`(278)·`Condition`(491)·`Observation`(2597)·`Procedure`(677)·
  `DiagnosticReport`(798)가 이미 이 패턴이다.
- 배포: `nest build` 후 `deploy.sh --api`.

> ⚠️ **twin도 동일한 벽에 있을 가능성이 높다.** twin의 `SMART_DEFAULT_SCOPE`
> (`twin-web/lib/smart.ts:6`)는 `patient/MedicationRequest.read`·`patient/AllergyIntolerance.read`를
> 요청하지만 해당 엔드포인트가 SMART 토큰을 거부한다. **공동 처리를 제안한다.**

### B. SMART 클라이언트 발급

`smart-client` 모듈(`POST`)로 confidential client 발급 요청.

| 항목 | 값 |
|---|---|
| `name` | `cerno` |
| `redirectUris` | `https://cerno.example.org/smart/callback` |
| `scopes` | §6의 9종 |
| 유형 | confidential (client_secret 발급) |

### C. 협의 항목

- [ ] 비식별화/마스킹 지원 범위 (§5)
- [ ] 감사로그 연계 방식 (§7)
- [ ] mTLS 적용 가능 여부
- [ ] 네트워크 경로/방화벽 (내부망 IP·포트)
- [ ] 담당자 및 일정

### D. 실측 확정 항목 (회신 불요)

| 항목 | 확정값 | 근거 |
|---|---|---|
| FHIR 버전·baseURL | R4 (4.0.1) · `/fhir/R4` | `fhir.controller.ts:51` |
| SMART App Launch(EHR launch) | **지원** · PKCE **S256** | `smart-auth.service.ts:135` |
| `capabilities` | `launch-ehr` · `launch-standalone` · `client-confidential-symmetric` · `sso-openid-connect` | 〃 :173 |
| 의료진/환자 컨텍스트 | `launch`·`openid`·`fhirUser` 스코프 지원 | 〃 :138 |
| 디스커버리 | `GET /.well-known/smart-configuration` | `smart-auth.controller.ts:79` |
| 스코프 정책 | 클라이언트 허용 scope와 요청 scope **교차 발급** | `smart-auth.service.ts:45` |
| `id_token` 서명 | **RS256 + JWKS**(2026-07-30 전환) | `.well-known` alg=RS256·jwks_uri |
| 조회 가능 리소스 | Patient·Condition·Observation·DiagnosticReport·Procedure ✅ / 나머지 3종은 A항 | `fhir.controller.ts` |
| Backend Services | 지원하나 **미채택**(§6) | 〃 |

---

## 11. 담당·일정·규제

- Cerno 파일럿 담당: (기입) · HIS 담당: (기입)
- 목표 일정: A항 가드 교체 + B항 클라이언트 발급 → 연동 테스트 → 파일럿
- 규제: 개인정보보호법·의료법 및 원내 정보보안/IRB 정책 준수.
  Cerno는 **별도 독립 보조 도구**이며 진단·처방을 자동 확정하지 않는다.
  **파일럿은 비임상 범위로 한정**한다(규제 범위 결정 전).
