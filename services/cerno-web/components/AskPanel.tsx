"use client";

// 질의 화면 — 백엔드 /ask 계약을 그대로 화면 상태로 옮긴다 (14_질의화면_UX기획.md §3).
// 계약에 있는 필드만 상태가 되고, 계약에 없는 상태를 만들지 않는다.
//
// clinician_id 는 화면 어디에도 없다 — 신원은 세션에서만 온다(불변 규칙 4).
// 질의·답변은 브라우저 밖에 저장하지 않는다(PHI 비저장 경계).

import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import s from "./ask.module.css";

type Visibility = "private" | "group" | "org";

type Source = { citation: string; visibility: Visibility; confidence: number };

type AskAnswer = {
  answer: string;
  grounded: boolean;
  sources: Source[];
  needs_human_review: boolean;
  review_reasons: string[];
  meta: Record<string, unknown>;
};

type Failure =
  | { kind: "busy"; retryAfter: number }
  | { kind: "session" }
  | { kind: "mismatch"; message: string }
  | { kind: "fail"; message: string };

const VIS_LABEL: Record<Visibility, string> = { private: "개인", group: "그룹", org: "공용" };

const UNGROUNDED = "근거로 확인되지 않습니다.";

type Usefulness = "helpful" | "harmless" | "misleading";
const USE_LABEL: Record<Usefulness, string> = {
  helpful: "도움됨", harmless: "무해", misleading: "오도",
};

/** 세션 내 문답 1건. 브라우저 메모리에만 있다 — 저장·전송하지 않는다(PHI 비저장 경계). */
type Exchange = { q: string; result: AskAnswer; correlationId: string };

type Risk = "high" | "medium" | "low";
type Suggestion = { question: string; category: string; risk: Risk; basis?: string };
const RISK_LABEL: Record<Risk, string> = { high: "위험도 높음", medium: "중간", low: "낮음" };
// 터치·스크린리더에서도 보이는 텍스트 라벨 — title 툴팁만으로는 인지 불가 (17 P1-3)
const RISK_TAG: Partial<Record<Risk, string>> = { high: "고위험", medium: "주의" };
// 카테고리 표시 순서 — 위해 가능성이 큰 주제부터
const CAT_ORDER = ["금기", "병용·상호작용", "용량·목표수치", "모니터링", "기타"];

// 서고에 실제 자료가 있는 주제 위주 — 빈 화면에서 첫 발을 떼게 한다 (환자 맥락 없을 때 폴백)
const EXAMPLES: Suggestion[] = [
  { question: "심부전 환자에게 어떤 약제를 병용해야 하나요?", category: "병용·상호작용", risk: "medium" },
  { question: "당뇨 환자의 목표 혈당은?", category: "용량·목표수치", risk: "low" },
  { question: "임신 중 피해야 할 약물은 무엇인가요?", category: "금기", risk: "high" },
];

/** 본문 앞의 DUR 경고 블록(`⚠️ …\n\n`)을 분리한다 — 경고 컴포넌트로 승격 렌더. */
function splitDur(answer: string): { dur: string | null; body: string } {
  if (!answer.startsWith("⚠️")) return { dur: null, body: answer };
  const cut = answer.indexOf("\n\n");
  if (cut < 0) return { dur: answer.replace(/^⚠️\s*/, ""), body: "" };
  return { dur: answer.slice(0, cut).replace(/^⚠️\s*/, ""), body: answer.slice(cut + 2) };
}

/** `[n]` 인용을 클릭 가능한 조각으로. 번호는 sources[] 순서와 1:1 계약이다. */
function withCitations(text: string, onJump: (n: number) => void) {
  const parts = text.split(/\[(\d+)\]/g);
  return parts.map((p, i) =>
    i % 2 === 1
      ? <button key={i} type="button" className={s.cite}
                onClick={() => onJump(Number(p))}>[{p}]</button>
      : <Fragment key={i}>{p}</Fragment>);
}

export default function AskPanel({ patientId }: { patientId?: string }) {
  const [query, setQuery] = useState("");
  const [asking, setAsking] = useState(false);
  const [elapsed, setElapsed] = useState(0);
  const [exchanges, setExchanges] = useState<Exchange[]>([]);
  const [selected, setSelected] = useState(-1);
  // "새 대화"는 기록을 지우지 않는다 — 이어서 질문의 맥락 범위만 여기서 끊는다.
  const [threadStart, setThreadStart] = useState(0);
  const threadLen = exchanges.length - threadStart;
  const [failure, setFailure] = useState<Failure | null>(null);
  const [countdown, setCountdown] = useState(0);
  const srcRefs = useRef<Record<number, HTMLDivElement | null>>({});
  // 진행 중 질의 취소 (17 P1-5) — 콜드 63초 실측이 있는 시스템에서 기다림 외의 선택지
  const abortRef = useRef<AbortController | null>(null);

  // 표시 대상은 선택된 문답에서 파생 — 계약(exchange) 밖의 결과 상태를 두지 않는다
  const current = selected >= 0 ? exchanges[selected] : null;
  const result = current?.result ?? null;
  const correlationId = current?.correlationId ?? "";
  const isLatest = selected === exchanges.length - 1;

  // 검수 (13_파일럿_운영계획.md §4) — 질의·답변 원문은 보내지 않는다
  const [usefulness, setUsefulness] = useState<Usefulness | null>(null);
  const [citationOk, setCitationOk] = useState<boolean | null>(null);
  const [missing, setMissing] = useState<boolean | null>(null);
  const [missingNote, setMissingNote] = useState("");
  // correlation_id 별 검수 완료 기록 — 기록 열람을 오가도 이중 검수를 막는다
  const [reviewed, setReviewed] = useState<Record<string, { priority: boolean }>>({});
  const reviewDone = correlationId ? reviewed[correlationId] ?? null : null;
  // 검수 제출 실패는 무음이면 안 된다 (17 P1-1) — 검수는 파일럿의 유일한 수집 행위다.
  const [reviewError, setReviewError] = useState<string | null>(null);
  const [reviewSending, setReviewSending] = useState(false);

  const resetReview = () => {
    setUsefulness(null); setCitationOk(null); setMissing(null); setMissingNote("");
    setReviewError(null);
  };

  const ask = useCallback(async (q: string) => {
    if (!q.trim() || asking) return;
    setAsking(true); setFailure(null); resetReview();
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const r = await fetch("/api/cerno/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: ac.signal,
        body: JSON.stringify({
          query: q,
          ...(patientId ? { patient_context: { patient_id: patientId } } : {}),
          // 이어서 질문 — 최근 3문답을 생성 문맥으로 보낸다(서버 계약 상한과 정합)
          history: exchanges.slice(threadStart).slice(-3).map((e) => ({
            query: e.q.slice(0, 500), answer: e.result.answer.slice(0, 4000),
          })),
        }),
      });
      if (r.ok) {
        const cid = r.headers.get("X-Correlation-Id") || "";
        const data: AskAnswer = await r.json();
        setExchanges((xs) => [...xs, { q, result: data, correlationId: cid }]);
        setSelected(exchanges.length);   // 새 항목 = 기존 길이 인덱스
        setQuery("");
        return;
      }
      if (r.status === 503) {
        const retry = Math.max(1, Number(r.headers.get("Retry-After")) || 5);
        setFailure({ kind: "busy", retryAfter: retry });
        setCountdown(retry);
        return;
      }
      if (r.status === 401) { setFailure({ kind: "session" }); return; }
      const body = await r.json().catch(() => ({}));
      if (r.status === 409) {
        setFailure({ kind: "mismatch", message: String(body?.detail || "환자 컨텍스트가 다릅니다.") });
        return;
      }
      setFailure({ kind: "fail", message: "답변을 가져오지 못했습니다. 잠시 후 다시 시도해 주세요." });
    } catch (e) {
      // 사용자 취소는 오류가 아니다 — 질문은 입력창에 그대로 남는다
      if (e instanceof DOMException && e.name === "AbortError") return;
      setFailure({ kind: "fail", message: "연결에 실패했습니다. 잠시 후 다시 시도해 주세요." });
    } finally {
      abortRef.current = null;
      setAsking(false);
    }
  }, [asking, patientId, exchanges, threadStart]);

  // 기록 열람 전환 시 검수 입력 초기화(완료 여부는 reviewed 맵이 보존)
  useEffect(() => { resetReview(); }, [selected]);

  // 환자 맞춤 추천 질문 — launch 진입 시 미리 생성해 제시한다(회당 최대 5개).
  // null = 첫 생성 중, [] = 실패/없음(정적 예시로 폴백). 세션 밖에 저장하지 않는다.
  const [suggested, setSuggested] = useState<Suggestion[] | null>(patientId ? null : []);
  const [topics, setTopics] = useState<string[]>([]);
  // 인구학 요약("남성, 49세") — /suggest 가 환자 맥락에서 뽑아준다 (17 P1-7).
  // 이름·등록번호는 처음부터 수집하지 않는다.
  const [patientSummary, setPatientSummary] = useState("");
  const [usedTopics, setUsedTopics] = useState<string[]>([]);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreExhausted, setMoreExhausted] = useState(false);
  // 카테고리별 관리 — 접기 상태 + 숨긴 질문(재생성 시 exclude 로 보내 재등장 방지)
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const [dismissed, setDismissed] = useState<string[]>([]);

  const dismissQuestion = (q: string) => {
    setSuggested((xs) => xs ? xs.filter((x) => x.question !== q) : xs);
    setDismissed((d) => [...d, q]);
  };

  // 답변 후 추천 재노출 (17 P1-2) — 접힌 스트립. 펼칠 때만 필요 시 재생성을 부른다
  // (자동 호출 금지 — GPU 예산 보호). 질문 클릭은 이어서 질문으로 전송된다.
  const [followOpen, setFollowOpen] = useState(false);

  const fetchSuggestions = useCallback(async (exclude: string[], focus = "") => {
    try {
      const r = await fetch("/api/cerno/suggest", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          patient_context: { patient_id: patientId }, exclude, focus,
        }),
      });
      const d = r.ok ? await r.json() : { questions: [], topics: [] };
      return {
        questions: (Array.isArray(d.questions) ? d.questions : []) as Suggestion[],
        topics: (Array.isArray(d.topics) ? d.topics : []) as string[],
        patientSummary: typeof d.patient_summary === "string" ? d.patient_summary : "",
      };
    } catch { return { questions: [], topics: [], patientSummary: "" }; }
  }, [patientId]);

  useEffect(() => {
    if (!patientId) return;
    let alive = true;
    void fetchSuggestions([]).then((d) => {
      if (!alive) return;
      setSuggested(d.questions);
      setTopics(d.topics);
      if (d.patientSummary) setPatientSummary(d.patientSummary);
    });
    return () => { alive = false; };
  }, [patientId, fetchSuggestions]);

  /** 생성 실패 시 재시도 — 환자 화면에서는 정적 예시로 폴백하지 않는다
      (환자 성별·나이와 무관한 예시가 맞춤 질문처럼 보이는 혼동 방지) */
  const retrySuggest = async () => {
    setSuggested(null);
    const d = await fetchSuggestions([]);
    setSuggested(d.questions);
    setTopics(d.topics);
    if (d.patientSummary) setPatientSummary(d.patientSummary);
  };

  /** 추가 생성 — focus 를 주면 그 확장 방향에 집중해 만든다 */
  const loadMore = async (focus = "") => {
    if (loadingMore || !suggested) return;
    setLoadingMore(true);
    if (focus) setUsedTopics((ts) => [...ts, focus]);
    const d = await fetchSuggestions(
      [...suggested.map((x) => x.question), ...dismissed], focus);
    const fresh = d.questions.filter((x) => !suggested.some((y) => y.question === x.question));
    if (fresh.length === 0 && !focus) setMoreExhausted(true);
    // 새 분석이 제시한 확장 방향은 합류(중복 제거)
    setTopics((ts) => [...ts, ...d.topics.filter((t) => !ts.includes(t))]);
    setSuggested((xs) => [...(xs ?? []), ...fresh]);
    setLoadingMore(false);
  };

  // 환자별 질의 이력 복원 — 서버 보관분(본인 것만)을 기록 레일에 되살린다.
  // 복원분은 열람용이다: threadStart 를 뒤로 밀어 새 질문의 맥락에 잇지 않는다.
  // 실패는 새 질의를 막지 않되 **한 줄로 알린다** (17 P1-1 — 무음 실패 금지).
  const [histFailed, setHistFailed] = useState(false);
  useEffect(() => {
    if (!patientId) return;
    let alive = true;
    (async () => {
      try {
        const r = await fetch(
          `/api/cerno/ask/history?patient_id=${encodeURIComponent(patientId)}`);
        if (!r.ok) { if (alive && r.status !== 401) setHistFailed(true); return; }
        const d = await r.json();
        const past: Exchange[] = (Array.isArray(d.entries) ? d.entries : []).map(
          (e: { query: string; answer: AskAnswer; correlation_id: string }) => ({
            q: e.query, result: e.answer, correlationId: e.correlation_id,
          }));
        if (alive && past.length > 0) {
          setExchanges((xs) => (xs.length === 0 ? past : xs));
          setThreadStart((t) => (t === 0 ? past.length : t));
        }
      } catch { if (alive) setHistFailed(true); }
    })();
    return () => { alive = false; };
  }, [patientId]);

  // 추천 생성 진행 바 — 질의 진행 바와 같은 점근 채움
  const suggestLoading = (patientId && suggested === null) || loadingMore;
  const [sugElapsed, setSugElapsed] = useState(0);
  useEffect(() => {
    if (!suggestLoading) { setSugElapsed(0); return; }
    const t0 = Date.now();
    const t = setInterval(() => setSugElapsed((Date.now() - t0) / 1000), 250);
    return () => clearInterval(t);
  }, [suggestLoading]);

  // 진행 표시(14_질의화면_UX기획.md §5 대기 투명성) — 응답은 단일 요청이라 실제
  // 단계 신호가 없다. 경과 시간으로 문구를 바꾸고, 바는 점근 채움(도착 전 100% 금지).
  useEffect(() => {
    if (!asking) { setElapsed(0); return; }
    const t0 = Date.now();
    const t = setInterval(() => setElapsed((Date.now() - t0) / 1000), 250);
    return () => clearInterval(t);
  }, [asking]);

  // 503 카운트다운 → 자동 재시도. 서버가 준 Retry-After 가 속도를 정한다.
  useEffect(() => {
    if (failure?.kind !== "busy") return;
    if (countdown <= 0) { void ask(query); return; }
    const t = setTimeout(() => setCountdown((c) => c - 1), 1000);
    return () => clearTimeout(t);
  }, [failure, countdown, ask, query]);

  const jump = (n: number) => {
    const el = srcRefs.current[n];
    el?.scrollIntoView({ behavior: "smooth", block: "center" });
    el?.classList.remove(s.srcFlash);
    // 리플로 강제로 재트리거
    void el?.offsetWidth;
    el?.classList.add(s.srcFlash);
  };

  const { dur, body } = result ? splitDur(result.answer) : { dur: null, body: "" };
  const isEmpty = result !== null && !result.grounded
    && result.sources.length === 0 && body.includes(UNGROUNDED);
  const hasSources = (result?.sources.length ?? 0) > 0;
  const reviewComplete = usefulness !== null && missing !== null
    && (!hasSources || citationOk !== null);

  const submitReview = async () => {
    if (!reviewComplete || !correlationId || reviewDone || reviewSending) return;
    setReviewSending(true); setReviewError(null);
    try {
      const r = await fetch("/api/cerno/review", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          correlation_id: correlationId,
          usefulness,
          citation_ok: hasSources ? citationOk : null,
          missing,
          missing_note: missing ? missingNote.slice(0, 300) : "",
        }),
      });
      if (r.ok) {
        const d = await r.json();
        setReviewed((m) => ({ ...m, [correlationId]: { priority: d.priority } }));
      } else if (r.status === 401) {
        setReviewError("세션이 만료됐습니다 — HIS에서 재실행 후 다시 기록해 주세요.");
      } else {
        setReviewError("검수 기록에 실패했습니다 — 다시 눌러 주세요.");
      }
    } catch {
      setReviewError("검수 기록에 실패했습니다 — 연결 확인 후 다시 눌러 주세요.");
    } finally {
      setReviewSending(false);
    }
  };

  return (
    <div className={s.layout}>
      {exchanges.length > 0 && (
        <aside className={s.rail} aria-label="질의 기록">
          <div className={s.railHead}>
            <span>기록 · {exchanges.length}</span>
            <button type="button" className={s.railNew}
                    disabled={threadLen === 0}
                    onClick={() => {
                      // 기록은 남긴다 — 이어서 맥락만 끊고 빈 질문 상태로 돌아간다
                      setThreadStart(exchanges.length);
                      setSelected(-1); setFailure(null);
                    }}>
              새 대화
            </button>
          </div>
          {/* 기록 범위 표기 — "환자마다? 의료진마다?" 를 화면이 스스로 답한다 */}
          <p className={s.railHint}>
            {patientId
              ? "이 환자에 대한 내 질의만 보관·표시됩니다 — 다른 의료진에게 보이지 않습니다."
              : "이 세션에서만 유지됩니다 — 환자 화면이 아닌 질의는 저장하지 않습니다."}
          </p>
          {exchanges.map((x, i) => (
            <Fragment key={i}>
              {i === threadStart && i > 0 && (
                <div className={s.railDivider} aria-hidden>── 새 대화 ──</div>
              )}
              <button type="button"
                      className={`${s.railItem} ${i === selected ? s.railItemOn : ""}`}
                      onClick={() => setSelected(i)}>
                <span className={s.railGlyph} role="img"
                      aria-label={x.result.answer.startsWith("⚠️") ? "DUR 경고 있음"
                        : x.result.grounded ? "근거 있음" : "근거 없음"}>
                  {x.result.answer.startsWith("⚠️") ? "⛔" : x.result.grounded ? "✓" : "∅"}
                </span>
                <span className={s.railQ}>{x.q}</span>
              </button>
            </Fragment>
          ))}
        </aside>
      )}

    <div className={s.panel}>
      {patientId && (
        <div className={s.ptBanner}>
          <span className={s.ptWho}>
            진료 중 환자 · Patient/{patientId}
            {patientSummary && <span className={s.ptDemo}> · {patientSummary}</span>}
          </span>
          <span className={s.ptHint}>질문에 이 환자의 투약·맥락이 자동 반영됩니다</span>
        </div>
      )}
      {histFailed && (
        <p className={s.degraded} role="status">
          이전 질의 기록을 불러오지 못했습니다 — 새 질문은 정상 동작합니다.
        </p>
      )}

      <form className={s.askBox}
            onSubmit={(e) => { e.preventDefault(); void ask(query); }}>
        <textarea value={query} rows={2}
                  onChange={(e) => setQuery(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); void ask(query); }
                  }}
                  placeholder={threadLen > 0
                    ? "이어서 질문하세요 — 직전 문답의 맥락이 함께 전달됩니다"
                    : "임상 지식을 질문하세요 — 서고의 근거로만 답합니다"} />
        <div className={s.askRow}>
          {threadLen > 0 && (
            <span className={s.followNote}>
              최근 {Math.min(threadLen, 3)}개 문답을 이어서 질문합니다
            </span>
          )}
          <button type="submit" className={s.primary} disabled={asking || !query.trim()}>
            {asking ? "질문 중…" : "질문"}
          </button>
        </div>
      </form>

      {threadLen === 0 && !result && !asking && !failure && (
        <div className={s.suggestArea}>
          {suggested !== null && suggested.length === 0 && patientId && !suggestLoading && (
            <div className={s.suggestHead}>
              <span className={s.exLabel}>이 환자 관련 질문을 만들지 못했습니다</span>
              <button type="button" className={s.moreBtn}
                      onClick={() => void retrySuggest()}>다시 시도</button>
            </div>
          )}
          {suggested !== null && (suggested.length > 0 || !patientId) && (() => {
            const items = suggested.length > 0 ? suggested : EXAMPLES;
            const cats = [...CAT_ORDER.filter((c) => items.some((x) => x.category === c)),
                          ...[...new Set(items.map((x) => x.category))]
                            .filter((c) => !CAT_ORDER.includes(c))];
            return (
              <>
                <div className={s.suggestHead}>
                  <span className={s.exLabel}>
                    {suggested.length > 0 ? "이 환자 관련 질문" : "예시 질문"}
                  </span>
                  {suggested.length > 0 && !moreExhausted && (
                    <button type="button" className={s.moreBtn}
                            disabled={loadingMore} onClick={() => void loadMore()}>
                      {loadingMore ? "생성 중…" : "질문 더 만들기"}
                    </button>
                  )}
                </div>
                {cats.map((cat) => {
                  const catItems = items.filter((x) => x.category === cat);
                  const isCol = Boolean(collapsed[cat]);
                  return (
                    <div key={cat} className={s.catGroup}>
                      <div className={s.catHead}>
                        <button type="button" className={s.catToggle}
                                aria-expanded={!isCol}
                                onClick={() => setCollapsed((m) => ({ ...m, [cat]: !m[cat] }))}>
                          <span aria-hidden>{isCol ? "▸" : "▾"}</span> {cat}
                          <span className={s.catCount}>{catItems.length}</span>
                        </button>
                        {suggested.length > 0 && (
                          <button type="button" className={s.catMore} disabled={loadingMore}
                                  title={`${cat} 질문 더 만들기`}
                                  onClick={() => void loadMore(cat)}>＋</button>
                        )}
                      </div>
                      {!isCol && (
                        <div className={s.catChips}>
                          {catItems.map((x) => (
                            <div key={x.question}
                                 className={`${s.exBtn} ${x.risk === "high" ? s.exHigh : x.risk === "medium" ? s.exMed : ""}`}>
                              <button type="button" className={s.exBody}
                                      title={`위험도: ${RISK_LABEL[x.risk]}`}
                                      onClick={() => { setQuery(x.question); void ask(x.question); }}>
                                <span className={s.exQ}>
                                  {x.risk === "high" && <span aria-hidden className={s.riskDot} />}
                                  {x.question}
                                  {RISK_TAG[x.risk] && (
                                    <span className={`${s.riskTag} ${x.risk === "high" ? s.riskTagHigh : ""}`}>
                                      {RISK_TAG[x.risk]}
                                    </span>
                                  )}
                                </span>
                                {x.basis && <span className={s.exBasis}>근거: {x.basis}</span>}
                              </button>
                              <button type="button" className={s.exDel} aria-label="질문 숨기기"
                                      title="숨기기 — 이후 재생성에서도 제외"
                                      onClick={() => dismissQuestion(x.question)}>✕</button>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
                {suggested.length > 0 && topics.length > 0 && (
                  <div className={s.topicRow}>
                    <span className={s.exLabel}>확장 방향</span>
                    {topics.map((t) => (
                      <button key={t} type="button"
                              className={`${s.topicBtn} ${usedTopics.includes(t) ? s.topicUsed : ""}`}
                              disabled={loadingMore}
                              onClick={() => void loadMore(t)}>
                        {t}
                      </button>
                    ))}
                  </div>
                )}
              </>
            );
          })()}
          {suggestLoading && (
            <div className={s.progressCard} role="status">
              <div className={s.progressTrack} aria-hidden>
                <div className={s.progressFill}
                     style={{ width: `${Math.min(95, 100 * (1 - Math.exp(-sugElapsed / 3))).toFixed(1)}%` }} />
              </div>
              <p className={s.progress}>
                {suggested === null ? "이 환자 관련 질문을 구성하는 중…" : "추가 질문을 만드는 중…"}
              </p>
            </div>
          )}
        </div>
      )}

      {asking && (
        <div className={s.progressCard} role="status">
          <div className={s.progressTrack} aria-hidden>
            {/* 점근 채움: 4초에 ~63%, 8초에 ~86% — 도착 전에는 95%를 넘지 않는다 */}
            <div className={s.progressFill}
                 style={{ width: `${Math.min(95, 100 * (1 - Math.exp(-elapsed / 4))).toFixed(1)}%` }} />
          </div>
          <p className={s.progress}>
            {elapsed < 1.5 ? "서고에서 근거 검색 중…"
              : elapsed < 8 ? "근거를 대조하고 답변 생성 중…"
              : "AI 서버 웜업으로 조금 늦어지고 있습니다 — 곧 완료됩니다…"}
          </p>
          <button type="button" className={s.ghost}
                  onClick={() => abortRef.current?.abort()}>
            취소
          </button>
        </div>
      )}

      {failure?.kind === "busy" && (
        <div className={s.busyCard} role="status">
          <strong>지금 여러 분이 사용 중입니다</strong>
          <p>원내 GPU를 함께 씁니다. {countdown}초 후 자동으로 다시 시도합니다.</p>
          <button type="button" className={s.ghost}
                  onClick={() => { setFailure(null); setCountdown(0); }}>
            재시도 중단
          </button>
        </div>
      )}
      {failure?.kind === "session" && (
        <div className={s.sessionCard} role="alert">
          <strong>세션이 만료됐습니다</strong>
          <p>HIS 화면에서 Cerno를 다시 실행해 주세요. 작성 중인 질문은 그대로 남아 있습니다.</p>
        </div>
      )}
      {(failure?.kind === "mismatch" || failure?.kind === "fail") && (
        <div className={s.failCard} role="alert">{failure.message}</div>
      )}

      {result && (
        <div className={s.answerCard} data-correlation-id={correlationId || undefined}>
          {dur && (
            <div className={s.durBanner} role="alert">
              <span className={s.durIcon} aria-hidden>⛔</span>
              <div>
                <strong>DUR — {dur}</strong>
                <p>식약처 정본 기준. 근거 문서 유무와 무관하게 표시됩니다.</p>
              </div>
            </div>
          )}

          {result.needs_human_review && (
            <div className={s.reviewBanner} role="alert">
              <span aria-hidden>⚠️</span>
              <div>
                <strong>검토 필요 — 원문과 기존 절차로 재확인하십시오</strong>
                {result.review_reasons.length > 0 && (
                  <ul>{result.review_reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
                )}
              </div>
            </div>
          )}

          {isEmpty ? (
            <div className={s.emptyCard}>
              <p className={s.emptyMsg}>{UNGROUNDED}</p>
              <p className={s.emptyWhy}>
                서고에 이 주제의 자료가 없습니다. Cerno는 근거 없이 답을 만들지 않습니다.
              </p>
              <a className={s.ghost}
                 href={`/portal?tab=library${patientId
                   ? `&return=${encodeURIComponent(`/patient/${patientId}`)}` : ""}`}>
                이 주제 자료 올리기 → 서고
              </a>
            </div>
          ) : (
            <>
              {body && <div className={s.answerBody}>{withCitations(body, jump)}</div>}

              {Boolean(result.meta?.degraded) && (
                <p className={s.degraded}>
                  일부 지식 저장소 조회 실패 — 결과가 불완전할 수 있습니다
                </p>
              )}

              {result.sources.length > 0 && (
                <div className={s.sources}>
                  <h4>근거 · 출처</h4>
                  {result.sources.map((src, i) => (
                    <div key={i} className={s.src}
                         ref={(el) => { srcRefs.current[i + 1] = el; }}>
                      <span className={s.srcN}>[{i + 1}]</span>
                      <span className={`${s.badge} ${s[src.visibility]}`}>
                        {VIS_LABEL[src.visibility]}
                      </span>
                      <span className={s.srcCite}>{src.citation}</span>
                      <span className={s.conf}>{src.confidence.toFixed(2)}</span>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {/* 검수는 최신 답변에서만 — 이미 기록된 건 열람 시 완료 표시 유지 */}
          {correlationId && (isLatest || reviewDone) && (
            <div className={s.reviewBox}>
              {reviewDone ? (
                <p className={s.reviewThanks} role="status">
                  {reviewDone.priority
                    ? "검수 우선 대상으로 기록됐습니다 — 파일럿 담당자에게도 공유해 주세요."
                    : "검수가 기록됐습니다. 감사합니다."}
                </p>
              ) : (
                <>
                  <div className={s.reviewQ}>
                    <span>이 답변은?</span>
                    {(Object.keys(USE_LABEL) as Usefulness[]).map((u) => (
                      <button key={u} type="button"
                              className={`${s.pill} ${usefulness === u ? (u === "misleading" ? s.pillBad : s.pillOn) : ""}`}
                              onClick={() => setUsefulness(u)}>{USE_LABEL[u]}</button>
                    ))}
                  </div>
                  {hasSources && (
                    <div className={s.reviewQ}>
                      <span>근거 인용이?</span>
                      <button type="button" className={`${s.pill} ${citationOk === true ? s.pillOn : ""}`}
                              onClick={() => setCitationOk(true)}>적절</button>
                      <button type="button" className={`${s.pill} ${citationOk === false ? s.pillBad : ""}`}
                              onClick={() => setCitationOk(false)}>부적절</button>
                    </div>
                  )}
                  <div className={s.reviewQ}>
                    <span>누락된 것?</span>
                    <button type="button" className={`${s.pill} ${missing === false ? s.pillOn : ""}`}
                            onClick={() => setMissing(false)}>없음</button>
                    <button type="button" className={`${s.pill} ${missing === true ? s.pillBad : ""}`}
                            onClick={() => setMissing(true)}>있음</button>
                    {missing === true && (
                      <input className={s.reviewNote} value={missingNote} maxLength={300}
                             onChange={(e) => setMissingNote(e.target.value)}
                             placeholder="무엇이 빠졌나요 (환자 정보는 쓰지 마세요)" />
                    )}
                  </div>
                  <div className={s.reviewActions}>
                    <button type="button" className={s.primary}
                            disabled={!reviewComplete || reviewSending}
                            onClick={() => void submitReview()}>
                      {reviewSending ? "기록 중…" : "검수 기록"}
                    </button>
                    {reviewError && (
                      <span className={s.reviewErr} role="alert">{reviewError}</span>
                    )}
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {patientId && result && isLatest && !asking && (
        <div className={s.followStrip}>
          <button type="button" className={s.catToggle} aria-expanded={followOpen}
                  onClick={() => {
                    const next = !followOpen;
                    setFollowOpen(next);
                    // 펼쳤는데 남은 추천이 없으면 그때만 생성한다
                    if (next && (!suggested || suggested.length === 0)) void retrySuggest();
                  }}>
            <span aria-hidden>{followOpen ? "▾" : "▸"}</span> 이어서 볼 질문
          </button>
          {followOpen && (
            <>
              {suggestLoading && <p className={s.progress}>질문을 만드는 중…</p>}
              {!suggestLoading && suggested && suggested.length > 0 && (
                <div className={s.catChips}>
                  {suggested.slice(0, 5).map((x) => (
                    <div key={x.question} className={`${s.exBtn} ${x.risk === "high" ? s.exHigh : x.risk === "medium" ? s.exMed : ""}`}>
                      <button type="button" className={s.exBody}
                              title={`위험도: ${RISK_LABEL[x.risk]}`}
                              onClick={() => { setQuery(x.question); void ask(x.question); }}>
                        <span className={s.exQ}>
                          {x.risk === "high" && <span aria-hidden className={s.riskDot} />}
                          {x.question}
                          {RISK_TAG[x.risk] && (
                            <span className={`${s.riskTag} ${x.risk === "high" ? s.riskTagHigh : ""}`}>
                              {RISK_TAG[x.risk]}
                            </span>
                          )}
                        </span>
                      </button>
                      <button type="button" className={s.exDel} aria-label="질문 숨기기"
                              onClick={() => dismissQuestion(x.question)}>✕</button>
                    </div>
                  ))}
                </div>
              )}
              {!suggestLoading && topics.length > 0 && (
                <div className={s.topicRow}>
                  <span className={s.exLabel}>확장 방향</span>
                  {topics.map((t) => (
                    <button key={t} type="button"
                            className={`${s.topicBtn} ${usedTopics.includes(t) ? s.topicUsed : ""}`}
                            disabled={loadingMore}
                            onClick={() => void loadMore(t)}>
                      {t}
                    </button>
                  ))}
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
    </div>
  );
}
