// SMART on FHIR EHR-launch 헬퍼 — PKCE·state 서명·smart-configuration·id_token 검증.
// confidential client. client_secret 는 서버 env(CERNO_SMART_CLIENT_SECRET)에만.
//
// 이식 원본: twin-web/lib/smart.ts (실제 보안 결함을 겪고 굳은 구현)
// 01_구현계획.md §3.2 불변 규칙 1·2·3 을 여기서 이행한다.
import { hmacHex, secret } from "@/lib/session";

/** 읽기 전용 9종. Cerno 는 HIS 에 write-back 하지 않는다.
 *  전부 HIS scopes_supported 에 이미 선언되어 있어 HIS 등록 작업이 불필요하다. */
export const SMART_DEFAULT_SCOPE =
  "launch openid fhirUser " +
  "patient/Patient.read patient/Condition.read patient/Observation.read " +
  "patient/DiagnosticReport.read patient/Procedure.read " +
  "patient/MedicationRequest.read patient/AllergyIntolerance.read " +
  "patient/DocumentReference.read";

export function publicBase(): string {
  return (process.env.CERNO_PUBLIC_BASE || "https://cerno.example.org").replace(/\/$/, "");
}
export function clientId(): string {
  return process.env.CERNO_SMART_CLIENT_ID || "cerno-weve-smart";
}
export function scope(): string {
  return process.env.CERNO_SMART_SCOPE || SMART_DEFAULT_SCOPE;
}

/** 불변 규칙 1 — 신뢰 issuer 호스트 화이트리스트.
 *  미적용 시 비신뢰 iss 로 discovery/token POST 를 유도해 client_secret 을 유출시킬 수 있다(SSRF). */
function allowedHosts(): string[] {
  return (process.env.CERNO_SMART_ISS_ALLOW || "his.example.org")
    .split(",").map((s) => s.trim().toLowerCase()).filter(Boolean);
}
export function isTrustedUrl(u: string | undefined): boolean {
  if (!u) return false;
  try {
    const h = new URL(u);
    return h.protocol === "https:" && allowedHosts().includes(h.hostname.toLowerCase());
  } catch { return false; }
}

function b64url(buf: ArrayBuffer | Uint8Array): string {
  const u8 = buf instanceof Uint8Array ? buf : new Uint8Array(buf);
  return Buffer.from(u8).toString("base64").replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export function randomToken(bytes = 32): string {
  const a = new Uint8Array(bytes);
  crypto.getRandomValues(a);
  return b64url(a);
}

/** PKCE S256: challenge = base64url(SHA256(verifier)) */
export async function pkceChallenge(verifier: string): Promise<string> {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier));
  return b64url(d);
}

/** state 쿠키: base64url(json).hmac — 런치↔콜백 사이 verifier·token endpoint 보존. */
export async function signState(obj: Record<string, unknown>): Promise<string> {
  const body = Buffer.from(JSON.stringify(obj)).toString("base64url");
  return `${body}.${await hmacHex(secret(), body)}`;
}
export async function readState(cookie: string | undefined): Promise<Record<string, any> | null> {
  if (!cookie) return null;
  const i = cookie.lastIndexOf(".");
  if (i < 0) return null;
  const body = cookie.slice(0, i), sig = cookie.slice(i + 1);
  if (sig !== (await hmacHex(secret(), body))) return null;
  try { return JSON.parse(Buffer.from(body, "base64url").toString()); } catch { return null; }
}

/** {iss}/.well-known/smart-configuration → {authorize, token, jwks} */
export async function discover(
  iss: string,
): Promise<{ authorize: string; token: string; jwks: string } | null> {
  try {
    const c = await fetch(`${iss.replace(/\/$/, "")}/.well-known/smart-configuration`,
      { cache: "no-store" }).then((r) => (r.ok ? r.json() : null));
    if (!c?.authorization_endpoint || !c?.token_endpoint) return null;
    return { authorize: c.authorization_endpoint, token: c.token_endpoint,
             jwks: c.jwks_uri || "" };
  } catch { return null; }
}

/** JWKS 캐시 — kid → CryptoKey. HIS 는 무중단 회전(KeyCustodyService)을 하므로 kid 로
 *  캐시하되, 못 찾으면 1회 재조회한다(회전 직후를 흡수). */
const _jwksCache = new Map<string, CryptoKey>();

async function _fetchJwk(jwksUri: string, kid: string): Promise<CryptoKey | null> {
  try {
    const doc = await fetch(jwksUri, { cache: "no-store" }).then((r) => (r.ok ? r.json() : null));
    for (const jwk of doc?.keys || []) {
      if (jwk.kid !== kid) continue;
      // RS256·서명용만 수용. alg/use 를 강제해 다른 용도 키 오용을 막는다.
      if (jwk.kty !== "RSA" || (jwk.alg && jwk.alg !== "RS256") || (jwk.use && jwk.use !== "sig")) {
        return null;
      }
      return await crypto.subtle.importKey(
        "jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    }
  } catch { /* fallthrough */ }
  return null;
}

async function _keyForKid(jwksUri: string, kid: string): Promise<CryptoKey | null> {
  const cached = _jwksCache.get(kid);
  if (cached) return cached;
  const k = await _fetchJwk(jwksUri, kid);
  if (k) _jwksCache.set(kid, k);
  return k;
}

/** 불변 규칙 2·3 — id_token(**RS256, JWKS**) 검증 → fhirUser 의 Practitioner id.
 *
 *  HIS v2026-07-30(RS256+JWKS 전환)에 맞춘다. 종전 HS256+client_secret 검증은
 *  HIS 가 시크릿을 해시로만 보관하게 되면서 원리적으로 불가해졌다(07_HIS_연동검증_회신.md).
 *
 *  서명·alg·kid·iss·aud(client_id)·exp 를 전부 검증한다. 하나라도 실패하면 "" 를 돌려주고
 *  콜백은 fail-closed(세션 미발급)한다.
 *
 *  ★ 반환값이 곧 clinician_id 이자 RAG 소유자 격리 키다. 따라서 `Practitioner/{id}`
 *    형태만 허용하고 **폴백을 두지 않는다** — 느슨한 파싱은 교차 누출로 직결된다.
 *
 *  주의: alg 를 헤더에서 읽되 **RS256 만 수용**한다. alg=none·HS256 을 받으면 안 된다
 *  (alg confusion — 공개키를 HMAC 키로 악용하는 고전 공격).
 */
export async function verifiedFhirUserUid(
  idToken: string | undefined, jwksUri: string, iss: string,
): Promise<string> {
  if (!idToken || !jwksUri) return "";
  try {
    const [h, p, s] = idToken.split(".");
    if (!h || !p || !s) return "";
    const hdr = JSON.parse(Buffer.from(h, "base64url").toString());
    if (hdr.alg !== "RS256" || !hdr.kid) return "";                                     // alg·kid
    const key = await _keyForKid(jwksUri, String(hdr.kid));
    if (!key) return "";                                                                // 미지 kid
    const ok = await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5", key,
      new Uint8Array(Buffer.from(s, "base64url")), new TextEncoder().encode(`${h}.${p}`));
    if (!ok) return "";                                                                 // 서명
    const pl = JSON.parse(Buffer.from(p, "base64url").toString());
    if (pl.iss && pl.iss !== iss) return "";                                            // iss
    if (pl.aud && !(Array.isArray(pl.aud) ? pl.aud : [pl.aud]).includes(clientId())) return "";  // aud
    if (pl.exp && Number(pl.exp) * 1000 < Date.now()) return "";                        // exp
    const m = String(pl.fhirUser || "").match(/(?:^|\/)Practitioner\/([A-Za-z0-9._-]+)$/);
    return m ? m[1] : "";
  } catch { return ""; }
}
