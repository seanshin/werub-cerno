// SMART on FHIR callback — authorize 리다이렉트 수신 → 토큰 교환(confidential+PKCE)
// → id_token 검증으로 clinician_id 확정 → per-user 토큰 보관 → Cerno 세션 발급.
//
// 이식 원본: twin-web/app/smart/callback/route.ts
// 01_구현계획.md §3.2 불변 규칙 2·3·9 를 여기서 이행한다.
import { NextRequest, NextResponse, after } from "next/server";
import { putSmartToken } from "@/lib/tokenStore";
import { issueSession, secret } from "@/lib/session";
import { clientId, isTrustedUrl, publicBase, readState, verifiedFhirUserUid } from "@/lib/smart";
import { warmModel } from "@/lib/warm";

function fail(reason: string) {
  return NextResponse.redirect(`${publicBase()}/launch-error?error=${reason}`);
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const code = searchParams.get("code");
  const state = searchParams.get("state");
  const err = searchParams.get("error");
  if (err) return fail(`smart_${err}`);

  const saved = await readState(req.cookies.get("cerno_smart")?.value);
  if (!code || !state || !saved || saved.state !== state) return fail("smart_state");
  // 토큰 엔드포인트 신뢰 재확인 — 서명 state 라 위조 불가이나 심층방어(불변 규칙 6)
  if (!isTrustedUrl(String(saved.token))) return fail("smart_untrusted_token");

  const cid = clientId();
  const cs = process.env.CERNO_SMART_CLIENT_SECRET || "";
  const body = new URLSearchParams({
    grant_type: "authorization_code",
    code,
    redirect_uri: `${publicBase()}/smart/callback`,
    client_id: cid,
    code_verifier: saved.verifier,
  });
  const headers: Record<string, string> = { "Content-Type": "application/x-www-form-urlencoded" };
  if (cs) headers["Authorization"] = `Basic ${Buffer.from(`${cid}:${cs}`).toString("base64")}`;

  let tok: any;
  try {
    const r = await fetch(String(saved.token), { method: "POST", headers, body, cache: "no-store" });
    tok = await r.json();
    if (!r.ok || !tok?.access_token) return fail("smart_token");
  } catch {
    return fail("smart_token_exc");
  }

  // 불변 규칙 2·3 — id_token(RS256·JWKS) 서명·iss·aud·exp 검증 → fhirUser 의 Practitioner id.
  // ★ 이 값이 clinician_id 이자 RAG 소유자 격리 키다. 실패 시 fail-closed.
  // jwks_uri 는 launch 가 서명 state 에 넣은 값 — 위조 불가.
  const uid = await verifiedFhirUserUid(tok.id_token, String(saved.jwks), String(saved.iss));
  if (!uid) return fail("smart_idtoken");

  // patient 컨텍스트: 토큰 응답 우선, 없으면 검증된 id_token 클레임(standalone 동의화면 선택분).
  // id_token 은 위에서 서명 검증했으므로 그 클레임은 신뢰할 수 있다.
  let patient = String(tok.patient || "");
  if (!patient && tok.id_token) {
    try {
      const claims = JSON.parse(Buffer.from(tok.id_token.split(".")[1], "base64url").toString());
      patient = String(claims.patient || "");
    } catch { /* 무시 — patient 없으면 포털로 */ }
  }

  // 불변 규칙 9 — per-user 토큰은 **인증된 저장소**에만. Cerno 는 write-back 하지 않으므로
  // 이 토큰의 용도는 FHIR read 전용이다.
  const ttl = Math.max(60, Number(tok.expires_in) || 3000);
  try {
    await putSmartToken(uid, { access_token: tok.access_token, patient, iss: String(saved.iss) }, ttl);
  } catch {
    // 저장 실패해도 로그인은 진행 — 조회 시 토큰 부재면 SMART 재진입을 요구한다
  }

  // 세션 확정 → 모델 예열. 응답 전송 후 실행(after)이라 launch 지연에 가산되지 않고,
  // 상주 해제 운영에서 첫 질의의 콜드(2.5~3.9초)를 미리 흡수한다. 실패는 무해하다.
  after(() => warmModel(uid));

  const { value } = await issueSession(uid, "clinician", secret());
  // 세션 TTL 이 SMART 토큰 TTL 을 넘으면 조회만 실패하는 구간이 생긴다(리스크 #8).
  // 그 구간은 fhir 커넥터가 감지해 재진입을 유도한다.
  const dest = patient ? `/patient/${encodeURIComponent(patient)}` : "/portal";
  const res = NextResponse.redirect(`${publicBase()}${dest}`);
  res.cookies.set("cerno_session", value,
    { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: Number(process.env.CERNO_SESSION_TTL || 28800) });
  res.cookies.delete("cerno_smart");
  return res;
}

export const dynamic = "force-dynamic";
