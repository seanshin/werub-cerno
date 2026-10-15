// 세션 쿠키 공용 헬퍼 — middleware.ts 와 동일 알고리즘.
// 쿠키: "{uid}.{role}.{exp}.{sig}", sig = HMAC-SHA256(secret, `sess:${uid}.${role}.${exp}`)
//
// 이식 원본: twin-web/lib/session.ts
// 불변 규칙 5(01_구현계획.md §3.2): 쿠키를 split 해서 신뢰하지 않는다 — 반드시 HMAC 재검증.
// 미검증 split 은 role 위조를 허용한다.
export const SESSION_TTL = Number(process.env.CERNO_SESSION_TTL || 8 * 3600);

export function secret(): string {
  return process.env.CERNO_SESSION_SECRET || "";
}

function toHex(buf: ArrayBuffer): string {
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export async function hmacHex(key: string, msg: string): Promise<string> {
  const k = await crypto.subtle.importKey("raw", new TextEncoder().encode(key),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
  return toHex(await crypto.subtle.sign("HMAC", k, new TextEncoder().encode(msg)));
}

/** 타이밍 비의존 비교 — 서명 비교에 == 를 쓰지 않는다. */
function safeEq(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let r = 0;
  for (let i = 0; i < a.length; i++) r |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return r === 0;
}

export type SessionInfo = { uid: string; role: string; exp: number };

/** 세션 쿠키 검증 → {uid, role, exp} 또는 null(만료/위조). uid = HIS Practitioner id. */
export async function verifySession(cookie: string | undefined, sec: string): Promise<SessionInfo | null> {
  if (!sec || !cookie) return null;
  const p = cookie.split(".");
  if (p.length < 4) return null;
  const [uid, role, exp, sig] = p;
  if (!/^\d+$/.test(exp) || Number(exp) * 1000 < Date.now()) return null;
  const ok = safeEq(sig, await hmacHex(sec, `sess:${uid}.${role}.${exp}`));
  return ok ? { uid, role, exp: Number(exp) } : null;
}

/** 새 세션 쿠키 값 발급. */
export async function issueSession(uid: string, role: string, sec: string): Promise<{ value: string; exp: number }> {
  const exp = Math.floor(Date.now() / 1000) + SESSION_TTL;
  const value = `${uid}.${role}.${exp}.${await hmacHex(sec, `sess:${uid}.${role}.${exp}`)}`;
  return { value, exp };
}
