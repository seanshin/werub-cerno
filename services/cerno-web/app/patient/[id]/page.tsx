// 진료 중 질의 화면 — 환자 맥락 launch 의 도착지.
// callback 이 patient 컨텍스트가 있으면 여기로 리다이렉트한다(이전에는 라우트가 없어 404).
//
// 환자 전환 UI 는 두지 않는다 — 백엔드가 launch 환자 불일치를 409 로 거부하는 것을
// 화면이 앞서 반영한다(14_질의화면_UX기획.md §2). 다른 환자는 그 환자 화면에서 실행한다.
import AskPanel from "@/components/AskPanel";
import s from "@/components/portal.module.css";

export default async function PatientAskPage(
  { params }: { params: Promise<{ id: string }> },
) {
  const { id } = await params;
  const patientId = decodeURIComponent(id);
  return (
    <main className={s.page}>
      <header className={s.head}>
        <h1>Cerno 질의</h1>
        <p className={s.sub}>서고의 근거로만 답합니다. 근거가 없으면 답을 만들지 않습니다.</p>
      </header>
      <AskPanel patientId={patientId} />
    </main>
  );
}
