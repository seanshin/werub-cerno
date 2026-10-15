// SMART on FHIR launch — HIS 가 ?iss=&launch= 로 진입 → authorize 리다이렉트(PKCE S256).
// launch 파라미터가 없으면 Standalone Launch(포털 진입)로 동작한다.
//
// 이식 원본: twin-web/app/smart/launch/route.ts
import { NextRequest, NextResponse } from "next/server";
import {
  clientId, discover, isTrustedUrl, pkceChallenge, publicBase, randomToken, scope, signState,
} from "@/lib/smart";

function fail(reason: string) {
  return NextResponse.redirect(`${publicBase()}/launch-error?error=${reason}`);
}

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  // iss 미지정 = 포털 직접 진입 → 기본 HIS 로 Standalone Launch
  const iss = searchParams.get("iss") || process.env.CERNO_SMART_DEFAULT_ISS || "";
  const launch = searchParams.get("launch") || "";
  if (!iss) return fail("smart_no_iss");

  // 불변 규칙 1 — 비신뢰 issuer 로 discovery/token POST(=client_secret 유출) 차단
  if (!isTrustedUrl(iss)) return fail("smart_untrusted_iss");

  const eps = await discover(iss);
  if (!eps || !isTrustedUrl(eps.authorize) || !isTrustedUrl(eps.token)) return fail("smart_discovery");
  // jwks_uri 도 신뢰 호스트여야 한다 — 비신뢰 URL 로 id_token 검증키를 가져오면
  // 공격자 키로 서명된 토큰을 통과시킬 수 있다.
  if (!eps.jwks || !isTrustedUrl(eps.jwks)) return fail("smart_no_jwks");

  const verifier = randomToken();
  const challenge = await pkceChallenge(verifier);
  const state = randomToken(16);
  const redirectUri = `${publicBase()}/smart/callback`;

  const authUrl = new URL(eps.authorize);
  const q = authUrl.searchParams;
  q.set("response_type", "code");
  q.set("client_id", clientId());
  q.set("redirect_uri", redirectUri);
  q.set("scope", scope());
  q.set("state", state);
  q.set("aud", iss);
  if (launch) q.set("launch", launch);          // 없으면 standalone
  q.set("code_challenge", challenge);
  q.set("code_challenge_method", "S256");

  const res = NextResponse.redirect(authUrl.toString());
  // state·verifier·token endpoint 를 서명 쿠키로 보존(위조 불가·10분 TTL·경로 제한)
  // jwks 도 서명 state 에 넣어 callback 이 위조 불가능한 값으로 검증키를 가져오게 한다
  res.cookies.set("cerno_smart", await signState({ state, verifier, token: eps.token, jwks: eps.jwks, iss }),
    { httpOnly: true, secure: true, sameSite: "lax", path: "/smart", maxAge: 600 });
  return res;
}

export const dynamic = "force-dynamic";
