// launch 직후 모델 예열 — cerno-api `POST /warm` 을 서버사이드에서 호출.
//
// AI 서버가 상주 해제(요청 시 탑재·KEEP_ALIVE 5m) 운영이라, 의료진이 launch 후
// 화면을 읽다가 첫 질의를 하면 콜드 2.5~3.9초가 얹힌다. launch 시점에 예열해 두면
// 그 창이 닫힌다. 워밍은 최적화다 — 어떤 실패도 launch 흐름에 영향을 주지 않는다.
import { hmacHex } from "./session";

const BASE = process.env.CERNO_API_INTERNAL || "http://cerno-api:8300";

export async function warmModel(uid: string): Promise<void> {
  const KEY = process.env.CERNO_API_KEY || "";
  if (!KEY) return;
  try {
    // 프록시와 동일한 신원 서명 계약(불변 규칙 7) — cerno-api 가 독립 검증한다.
    const exp = Math.floor(Date.now() / 1000) + 60;
    const sig = await hmacHex(KEY, `${uid}.clinician.${exp}`);
    await fetch(`${BASE}/warm`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${KEY}`,
        "Content-Type": "application/json",
        "X-Cerno-User": uid,
        "X-Cerno-Role": "clinician",
        "X-Cerno-Sig": sig,
        "X-Cerno-Exp": String(exp),
      },
      cache: "no-store",
    });
  } catch {
    /* 워밍 실패는 무시 — 첫 질의가 콜드를 흡수할 뿐이다 */
  }
}
