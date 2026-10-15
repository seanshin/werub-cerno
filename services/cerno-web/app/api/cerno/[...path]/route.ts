// cerno-api 서버사이드 프록시 — API 키를 서버에서만 주입(브라우저 노출 방지).
// 브라우저는 동일 출처 /api/cerno/* 호출 → Next 서버가 신원을 서명해 cerno-api 로 전달.
//
// 이식 원본: twin-web/app/api/twin/[...path]/route.ts
// 01_구현계획.md §3.2 불변 규칙 4·5·6·7·8 을 여기서 이행한다.
import { NextRequest } from "next/server";
import { hmacHex, secret, verifySession } from "@/lib/session";
import { badSegments } from "@/lib/pathGuard";

const BASE = process.env.CERNO_API_INTERNAL || "http://cerno-api:8300";
const KEY = process.env.CERNO_API_KEY || "";

function err(code: string, message: string, status: number) {
  return new Response(JSON.stringify({ error: { code, message } }),
    { status, headers: { "Content-Type": "application/json" } });
}

async function handler(req: NextRequest, ctx: { params: Promise<{ path: string[] }> }) {
  const { path } = await ctx.params;

  // 불변 규칙 8 — 2차 게이트. fetch URL 정규화로 인한 인증 우회 차단(twin 실사례).
  if (badSegments(path)) return err("BAD_PATH", "잘못된 경로", 400);

  // 불변 규칙 5·6 — 쿠키 split 을 신뢰하지 않고 HMAC 재검증. 미들웨어가 1차이나
  // 프록시도 독립 검증해 단일 실패점을 제거한다.
  const info = await verifySession(req.cookies.get("cerno_session")?.value, secret());
  if (!info) return err("UNAUTHENTICATED", "HIS 경유 접근 필요", 401);

  const { uid, role } = info;

  // 불변 규칙 4 — clinician_id 는 요청 본문이 아니라 검증된 세션에서만 온다.
  // 본문에 실려 오고 세션과 다르면 403(백엔드에서 재확인하되 여기서 조기 차단).
  let body: string | undefined;
  if (req.method !== "GET" && req.method !== "HEAD") {
    body = await req.text();
    if (body) {
      try {
        const parsed = JSON.parse(body);
        if (typeof parsed?.clinician_id === "string" && parsed.clinician_id !== uid) {
          return err("CLINICIAN_ID_MISMATCH", "clinician_id 는 지정할 수 없습니다", 403);
        }
      } catch { /* JSON 이 아니면 통과 — 백엔드가 판단 */ }
    }
  }

  // 불변 규칙 7 — 신원 헤더 HMAC 서명 + exp 60초. 백엔드가 위조 X-Cerno-* 를 거부한다.
  const exp = Math.floor(Date.now() / 1000) + 60;
  const sig = await hmacHex(KEY, `${uid}.${role}.${exp}`);

  const target = `${BASE}/${path.join("/")}${new URL(req.url).search}`;
  const init: RequestInit = {
    method: req.method,
    headers: {
      Authorization: `Bearer ${KEY}`,
      "Content-Type": "application/json",
      "X-Cerno-User": uid,
      "X-Cerno-Role": role,
      "X-Cerno-Sig": sig,
      "X-Cerno-Exp": String(exp),
    },
    cache: "no-store",
  };
  if (body !== undefined) init.body = body;

  try {
    const r = await fetch(target, init);
    const ct = r.headers.get("content-type") || "application/json";
    if (ct.includes("text/event-stream") && r.body) {
      return new Response(r.body, {
        status: r.status,
        headers: { "Content-Type": ct, "Cache-Control": "no-cache", "X-Accel-Buffering": "no" },
      });
    }
    const buf = await r.arrayBuffer();
    // 헤더는 허용 목록만 통과 — 백엔드 내부 헤더를 브라우저에 흘리지 않는다.
    // Retry-After 는 503 재시도 카운트다운, X-Correlation-Id 는 검수 기록의 연결 키.
    const headers: Record<string, string> = { "Content-Type": ct };
    for (const h of ["Retry-After", "X-Correlation-Id"]) {
      const v = r.headers.get(h);
      if (v) headers[h] = v;
    }
    return new Response(buf, { status: r.status, headers });
  } catch {
    // 외부 에러 원문을 노출하지 않는다(내부 경로·라이브러리 문자열 유출 방지)
    return err("UPSTREAM_UNAVAILABLE", "일시적으로 처리할 수 없습니다", 502);
  }
}

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;

export const dynamic = "force-dynamic";
