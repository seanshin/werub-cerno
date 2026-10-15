// 포털 — 질의(기본) | 서고 탭 (14_질의화면_UX기획.md §2).
// 환자 없는 launch 의 도착지. 탭은 쿼리 파라미터라 ungrounded CTA 가
// `/portal?tab=library` 로 딥링크할 수 있다.
import AskPanel from "@/components/AskPanel";
import LibraryPanel from "@/components/LibraryPanel";
import s from "@/components/portal.module.css";

export default async function PortalPage(
  { searchParams }: { searchParams: Promise<{ tab?: string; return?: string }> },
) {
  const { tab, return: ret } = await searchParams;
  const library = tab === "library";
  // 복귀 동선 (17 P1-6) — 환자 화면에서 온 경우만. 열린 리다이렉트 방지로 경로를 제한한다
  const returnTo = ret && /^\/patient\/[^/]+$/.test(ret) ? ret : undefined;
  return (
    <main className={s.page}>
      <header className={s.head}>
        <h1>Cerno</h1>
        <nav className={s.tabs} aria-label="포털 탭">
          <a href="/portal" className={library ? s.tab : s.tabOn}>질의</a>
          <a href="/portal?tab=library" className={library ? s.tabOn : s.tab}>서고</a>
        </nav>
      </header>
      {library ? <LibraryPanel returnTo={returnTo} /> : (
        <>
          <p className={s.sub}>서고의 근거로만 답합니다. 근거가 없으면 답을 만들지 않습니다.</p>
          <AskPanel />
        </>
      )}
    </main>
  );
}
