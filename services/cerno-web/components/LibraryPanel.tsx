"use client";

// Cerno Library 서고 패널 — 업로드·공개범위·출처·도메인 지정 → 검증 → 적재.
// (P2 에서 /portal 페이지 본문을 컴포넌트로 추출 — 포털은 질의|서고 탭 래퍼가 됐다)
//
// 설계 의도: **적재 전에 위반 사유를 전부 보여준다.** 거버넌스는 거부이므로, 올린 뒤
// 실패를 통보하면 사용자가 무엇을 고쳐야 하는지 모른 채 반복하게 된다.
// clinician_id 는 화면에 없다 — 신원은 HIS launch 세션에서만 온다.

import { useCallback, useEffect, useState } from "react";
import s from "./portal.module.css";

type Visibility = "private" | "group" | "org";

type Doc = {
  doc_id: string; title: string; visibility: Visibility;
  chunks: number; groups: string[]; mine: boolean; updated_at: number;
};

const MODALITIES = ["text", "xray", "ct", "mri", "us", "pet",
                    "pathology", "endoscopy", "fundus", "ecg", "lab"];

const VIS_LABEL: Record<Visibility, string> = {
  private: "개인 서고 — 나만",
  group: "그룹 서가 — 소속 그룹",
  org: "공용 서고 — 기관 전체",
};

async function api(path: string, init?: RequestInit) {
  const r = await fetch(`/api/cerno${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
  });
  const body = await r.json().catch(() => ({}));
  return { ok: r.ok, status: r.status, body };
}

export default function LibraryPanel({ returnTo }: { returnTo?: string }) {
  const [docId, setDocId] = useState("");
  const [title, setTitle] = useState("");
  const [visibility, setVisibility] = useState<Visibility>("private");
  const [groups, setGroups] = useState("");
  const [specialty, setSpecialty] = useState("");
  const [modalities, setModalities] = useState<string[]>([]);
  const [icd, setIcd] = useState("");
  const [phiReviewed, setPhiReviewed] = useState(false);
  const [licenseApproved, setLicenseApproved] = useState(false);
  const [authors, setAuthors] = useState("");
  const [publication, setPublication] = useState("");
  const [year, setYear] = useState("");
  const [doi, setDoi] = useState("");
  const [url, setUrl] = useState("");
  const [text, setText] = useState("");

  const [violations, setViolations] = useState<string[] | null>(null);
  const [preview, setPreview] = useState<{ chunks: number; collection: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [docs, setDocs] = useState<Doc[]>([]);

  const manifest = useCallback(() => ({
    doc_id: docId.trim(),
    title: title.trim(),
    visibility,
    groups: visibility === "group"
      ? groups.split(",").map((g) => g.trim()).filter(Boolean) : [],
    specialty: specialty.trim(),
    modalities,
    icd_codes: icd.split(",").map((c) => c.trim().toUpperCase()).filter(Boolean),
    phi_reviewed: phiReviewed,
    license_approved: licenseApproved,
    source: {
      authors: authors.split(",").map((a) => a.trim()).filter(Boolean),
      publication: publication.trim(),
      year: year.trim() ? Number(year) : null,
      doi: doi.trim(),
      url: url.trim(),
    },
  }), [docId, title, visibility, groups, specialty, modalities, icd,
       phiReviewed, licenseApproved, authors, publication, year, doi, url]);

  // 목록 로드 실패는 빈 목록과 구별해 표시한다 (17 P1-1 — 무음 실패 금지)
  const [listError, setListError] = useState(false);
  const loadDocs = useCallback(async () => {
    try {
      const { ok, body } = await api("/library/documents");
      if (ok) { setDocs(body.documents || []); setListError(false); return; }
      setListError(true);
    } catch { setListError(true); }
  }, []);

  useEffect(() => { void loadDocs(); }, [loadDocs]);

  // 세션 만료 안내 (17 P1-8) — 작성 내용은 화면에 그대로 남는다. 리다이렉트 금지:
  // 폼 상태가 곧 문서 초안이고, 브라우저 저장소에 사본을 두지 않는 것이 PHI 경계다.
  const SESSION_MSG =
    "세션이 만료됐습니다. 작성 내용은 이 화면에 남아 있습니다 — " +
    "HIS에서 Cerno를 다시 실행한 뒤 이 탭에서 다시 제출해 주세요.";

  async function onValidate() {
    setBusy(true); setNotice("");
    const { ok, status, body } = await api("/library/validate", {
      method: "POST", body: JSON.stringify({ manifest: manifest(), text }),
    });
    setBusy(false);
    if (status === 401) { setNotice(SESSION_MSG); return; }
    if (!ok) { setViolations(body?.detail?.violations || ["검증 요청 실패"]); return; }
    setViolations(body.violations);
    setPreview({ chunks: body.chunk_preview, collection: body.collection });
  }

  async function onIngest() {
    setBusy(true); setNotice("");
    const { ok, status, body } = await api("/library/documents", {
      method: "POST", body: JSON.stringify({ manifest: manifest(), text }),
    });
    setBusy(false);
    if (ok) {
      setNotice(`적재 완료 — ${body.collection} · 청크 ${body.chunks}개` +
                (body.groups_expanded > 1 ? ` · 그룹 ${body.groups_expanded}곳` : ""));
      setViolations([]); setText(""); setDocId(""); setTitle("");
      void loadDocs();
      return;
    }
    if (status === 401) { setNotice(SESSION_MSG); return; }
    if (status === 422) { setViolations(body?.detail?.violations || []); return; }
    if (status === 502 && body?.detail?.partial) {
      setNotice("일부만 적재됐습니다. 같은 내용으로 다시 시도하면 안전하게 덮어씁니다.");
      setViolations(body.detail.errors || []);
      return;
    }
    setNotice(status === 503 ? "AI 서버가 혼잡합니다. 잠시 후 다시 시도해 주세요."
                             : "적재에 실패했습니다.");
  }

  async function onDelete(id: string) {
    setBusy(true);
    await api(`/library/documents/${encodeURIComponent(id)}`, { method: "DELETE" });
    setBusy(false);
    void loadDocs();
  }

  async function onFile(f: File | undefined) {
    if (!f) return;
    setText(await f.text());
    if (!title) setTitle(f.name.replace(/\.[^.]+$/, ""));
  }

  const needsSource = visibility === "org";

  return (
    <>
      {returnTo && (
        <p className={s.returnBar}>
          <a href={returnTo}>← 진료 화면으로 돌아가기</a>
          <span> — 같은 환자 컨텍스트로 복귀합니다</span>
        </p>
      )}
      <p className={s.sub}>
        문서를 공개범위별로 나누어 적재합니다. 적재 전에 거버넌스 검증을 통과해야 합니다.
      </p>

      <section className={s.card}>
        <h2>문서 등록</h2>

        <div className={s.grid}>
          <label className={s.field}>
            <span>문서 ID <em>영숫자 . _ -</em></span>
            <input value={docId} onChange={(e) => setDocId(e.target.value)}
                   placeholder="hf-guideline-2025" />
          </label>
          <label className={s.field}>
            <span>제목</span>
            <input value={title} onChange={(e) => setTitle(e.target.value)} />
          </label>
        </div>

        <fieldset className={s.fieldset}>
          <legend>공개범위</legend>
          {(Object.keys(VIS_LABEL) as Visibility[]).map((v) => (
            <label key={v} className={s.radio}>
              <input type="radio" name="visibility" checked={visibility === v}
                     onChange={() => setVisibility(v)} />
              <span>{VIS_LABEL[v]}</span>
            </label>
          ))}
          {visibility === "group" && (
            <label className={s.field}>
              <span>그룹 <em>쉼표 구분 · 소속 그룹만</em></span>
              <input value={groups} onChange={(e) => setGroups(e.target.value)}
                     placeholder="cardio-dept, research-a" />
            </label>
          )}
        </fieldset>

        {needsSource && (
          <fieldset className={s.fieldset}>
            <legend>출처 <em>공용 서고는 필수</em></legend>
            <div className={s.grid}>
              <label className={s.field}><span>저자 <em>쉼표 구분</em></span>
                <input value={authors} onChange={(e) => setAuthors(e.target.value)} /></label>
              <label className={s.field}><span>게재지</span>
                <input value={publication} onChange={(e) => setPublication(e.target.value)} /></label>
              <label className={s.field}><span>연도</span>
                <input value={year} inputMode="numeric"
                       onChange={(e) => setYear(e.target.value)} /></label>
              <label className={s.field}><span>DOI</span>
                <input value={doi} onChange={(e) => setDoi(e.target.value)} /></label>
              <label className={s.field}><span>URL</span>
                <input value={url} onChange={(e) => setUrl(e.target.value)} /></label>
            </div>
            <label className={s.check}>
              <input type="checkbox" checked={licenseApproved}
                     onChange={(e) => setLicenseApproved(e.target.checked)} />
              <span>저작권 검토를 마쳤습니다 <em>라이선스가 원내 활용을 허용함</em></span>
            </label>
          </fieldset>
        )}

        <fieldset className={s.fieldset}>
          <legend>도메인 태그 <em>검색 정밀화에 쓰입니다</em></legend>
          <div className={s.grid}>
            <label className={s.field}><span>진료과</span>
              <input value={specialty} onChange={(e) => setSpecialty(e.target.value)}
                     placeholder="cardiology" /></label>
            <label className={s.field}><span>상병 코드 <em>KCD/ICD-10 · 쉼표 구분</em></span>
              <input value={icd} onChange={(e) => setIcd(e.target.value)}
                     placeholder="I21, E11.9" /></label>
          </div>
          <div className={s.chips}>
            {MODALITIES.map((m) => (
              <label key={m} className={`${s.chip} ${modalities.includes(m) ? s.chipOn : ""}`}>
                <input type="checkbox" checked={modalities.includes(m)}
                       onChange={(e) => setModalities((prev) =>
                         e.target.checked ? [...prev, m] : prev.filter((x) => x !== m))} />
                {m}
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className={s.fieldset}>
          <legend>본문</legend>
          <input type="file" accept=".txt,.md,.csv"
                 onChange={(e) => void onFile(e.target.files?.[0])} />
          <textarea value={text} onChange={(e) => setText(e.target.value)} rows={10}
                    placeholder="본문을 붙여넣거나 파일을 선택하세요." />
          <p className={s.note}>
            본문은 Cerno에 저장되지 않습니다 — 청킹 후 원내 AI 서버 지식베이스로만 전달됩니다.
          </p>
        </fieldset>

        <label className={s.check}>
          <input type="checkbox" checked={phiReviewed}
                 onChange={(e) => setPhiReviewed(e.target.checked)} />
          <span><strong>비식별화를 확인했습니다</strong>
            <em>환자 성명·등록번호·연락처 등 식별정보가 본문에 없음</em></span>
        </label>

        <div className={s.actions}>
          <button type="button" onClick={onValidate} disabled={busy} className={s.secondary}>
            검증만 하기
          </button>
          <button type="button" onClick={onIngest}
                  disabled={busy || (violations !== null && violations.length > 0)}
                  className={s.primary}>
            지식베이스 적재
          </button>
        </div>

        {violations !== null && violations.length > 0 && (
          <div className={s.violations} role="alert">
            <strong>적재할 수 없습니다 — {violations.length}건</strong>
            <ul>{violations.map((v, i) => <li key={i}>{v}</li>)}</ul>
          </div>
        )}
        {violations !== null && violations.length === 0 && preview && (
          <div className={s.pass} role="status">
            검증 통과 — <code>{preview.collection}</code>에 청크 {preview.chunks}개로 적재됩니다.
          </div>
        )}
        {notice && <div className={s.notice} role="status">{notice}</div>}
      </section>

      <section className={s.card}>
        <h2>등록된 문서 <span className={s.count}>{docs.length}</span></h2>
        {listError ? (
          <p className={s.empty} role="alert">
            문서 목록을 불러오지 못했습니다.{" "}
            <button type="button" className={s.secondary}
                    onClick={() => void loadDocs()}>다시 시도</button>
          </p>
        ) : docs.length === 0 ? (
          <p className={s.empty}>접근 가능한 문서가 없습니다.</p>
        ) : (
          <table className={s.table}>
            <thead>
              <tr><th>제목</th><th>공개범위</th><th>청크</th><th /></tr>
            </thead>
            <tbody>
              {docs.map((d) => (
                <tr key={d.doc_id}>
                  <td>
                    <span className={s.docTitle}>{d.title}</span>
                    <span className={s.docId}>{d.doc_id}</span>
                  </td>
                  <td>
                    <span className={`${s.badge} ${s[d.visibility]}`}>{d.visibility}</span>
                    {d.groups.length > 0 && <span className={s.groups}>{d.groups.join(", ")}</span>}
                  </td>
                  <td className={s.num}>{d.chunks}</td>
                  <td>
                    {d.mine && (
                      <button type="button" className={s.danger} disabled={busy}
                              onClick={() => void onDelete(d.doc_id)}>삭제</button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>
    </>
  );
}
