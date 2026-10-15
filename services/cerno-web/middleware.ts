// 1차 인증 게이트 — 세션 없는 접근을 SMART launch 로 돌린다.
// 프록시(app/api/cerno/[...path])가 2차로 독립 검증한다(불변 규칙 6: 단일 실패점 제거).
//
// 이식 원본: twin-web/middleware.ts
import { NextRequest, NextResponse } from "next/server";
import { hasTraversal } from "@/lib/pathGuard";

const SESSION_COOKIE = "cerno_session";

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(key: string, msg: string): Promise<string> {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return toHex(await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(msg)));
}

function safeEq(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

/** 불변 규칙 5 — split 결과를 신뢰하지 않고 HMAC 재검증한다(role 위조 차단). */
async function validSession(cookie: string | undefined, sec: string): Promise<boolean> {
  if (!sec || !cookie) return false;
  const p = cookie.split(".");
  if (p.length < 4) return false;
  const [uid, role, exp, sig] = p;
  if (!/^\d+$/.test(exp) || Number(exp) * 1000 < Date.now()) return false;
  return safeEq(sig, await hmacHex(sec, `sess:${uid}.${role}.${exp}`));
}

export async function middleware(req: NextRequest) {
  const { pathname } = req.nextUrl;

  // 불변 규칙 8 — traversal 1차 게이트
  if (hasTraversal(pathname)) {
    return new NextResponse(JSON.stringify({ error: { code: "BAD_PATH", message: "잘못된 경로" } }),
      { status: 400, headers: { "Content-Type": "application/json" } });
  }

  // 세션 발급 전 진입점만 예외. Cerno 는 자체 로그인 화면을 두지 않는다 —
  // 신원 출처를 HIS 하나로 단일화한다(01_구현계획.md §3.2 포털 경로).
  if (pathname.startsWith("/smart")
      || pathname.startsWith("/launch-error")
      || pathname.startsWith("/_next")
      || pathname === "/favicon.ico") {
    return NextResponse.next();
  }

  const sec = process.env.CERNO_SESSION_SECRET || "";
  if (await validSession(req.cookies.get(SESSION_COOKIE)?.value, sec)) {
    return NextResponse.next();
  }

  if (pathname.startsWith("/api/")) {
    return new NextResponse(
      JSON.stringify({ error: { code: "UNAUTHENTICATED", message: "HIS 경유 접근 필요" } }),
      { status: 401, headers: { "Content-Type": "application/json" } });
  }

  // 세션 없음 → SMART launch 로(standalone). 자체 로그인 폼 없음.
  const url = req.nextUrl.clone();
  url.pathname = "/smart/launch";
  url.search = "";
  return NextResponse.redirect(url);
}

export const config = { matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"] };
