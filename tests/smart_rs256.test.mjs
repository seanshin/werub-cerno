// RS256+JWKS id_token 검증 계약 (HIS v2026-07-30 전환).
// 실행: node tests/smart_rs256.test.mjs
//
// 여기서 고정하는 것: alg confusion 방어 · kid 불일치 거부 · 위조 서명 거부 ·
//   iss/aud/exp 검증 · Practitioner 엄격 파싱(폴백 없음).
// crypto.subtle 만 쓰므로 별도 의존성이 없다. verifiedFhirUserUid 로직을 그대로 재현한다.

import assert from "node:assert";

const b64u = (buf) => Buffer.from(buf).toString("base64url");
const CLIENT_ID = "test-client";
const ISS = "https://his.example";

// 테스트용 RSA 키쌍
const kp = await crypto.subtle.generateKey(
  { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
  true, ["sign", "verify"]);
const pubJwk = await crypto.subtle.exportKey("jwk", kp.publicKey);
pubJwk.kid = "test-kid-1"; pubJwk.alg = "RS256"; pubJwk.use = "sig";

async function sign(header, payload, key = kp.privateKey) {
  const h = b64u(JSON.stringify(header)), p = b64u(JSON.stringify(payload));
  const sig = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, new TextEncoder().encode(`${h}.${p}`));
  return `${h}.${p}.${b64u(sig)}`;
}

// verifiedFhirUserUid 를 이식(lib/smart.ts 와 동일 로직). JWKS 는 로컬 주입.
const JWKS = { keys: [pubJwk] };
async function verify(idToken, { iss = ISS, aud = CLIENT_ID } = {}) {
  try {
    const [h, p, s] = idToken.split(".");
    if (!h || !p || !s) return "";
    const hdr = JSON.parse(Buffer.from(h, "base64url").toString());
    if (hdr.alg !== "RS256" || !hdr.kid) return "";
    const jwk = JWKS.keys.find((k) => k.kid === hdr.kid);
    if (!jwk || jwk.kty !== "RSA" || (jwk.alg && jwk.alg !== "RS256") || (jwk.use && jwk.use !== "sig")) return "";
    const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
    const ok = await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key,
      new Uint8Array(Buffer.from(s, "base64url")), new TextEncoder().encode(`${h}.${p}`));
    if (!ok) return "";
    const pl = JSON.parse(Buffer.from(p, "base64url").toString());
    if (pl.iss && pl.iss !== iss) return "";
    if (pl.aud && !(Array.isArray(pl.aud) ? pl.aud : [pl.aud]).includes(aud)) return "";
    if (pl.exp && Number(pl.exp) * 1000 < Date.now()) return "";
    const m = String(pl.fhirUser || "").match(/(?:^|\/)Practitioner\/([A-Za-z0-9._-]+)$/);
    return m ? m[1] : "";
  } catch { return ""; }
}

const future = Math.floor(Date.now() / 1000) + 3600;
const past = Math.floor(Date.now() / 1000) - 10;
const H = { alg: "RS256", kid: "test-kid-1", typ: "JWT" };
let n = 0;
const t = async (name, fn) => { await fn(); console.log(`  ✅ ${name}`); n++; };

// 정상
await t("유효 토큰 → Practitioner id", async () => {
  const uid = await verify(await sign(H, { iss: ISS, aud: CLIENT_ID, exp: future, fhirUser: "Practitioner/dr-kim" }));
  assert.strictEqual(uid, "dr-kim");
});

// 🔴 alg confusion — HS256/none 거부
await t("alg=HS256 헤더 거부 (alg confusion)", async () => {
  const forged = b64u(JSON.stringify({ alg: "HS256", kid: "test-kid-1" })) + "." +
                 b64u(JSON.stringify({ fhirUser: "Practitioner/x", exp: future })) + "." + b64u("sig");
  assert.strictEqual(await verify(forged), "");
});
await t("alg=none 거부", async () => {
  const forged = b64u(JSON.stringify({ alg: "none", kid: "test-kid-1" })) + "." +
                 b64u(JSON.stringify({ fhirUser: "Practitioner/x", exp: future })) + ".";
  assert.strictEqual(await verify(forged), "");
});

// kid
await t("미지 kid 거부", async () => {
  assert.strictEqual(await verify(await sign({ alg: "RS256", kid: "unknown" }, { fhirUser: "Practitioner/x", exp: future })), "");
});
await t("kid 없는 헤더 거부", async () => {
  assert.strictEqual(await verify(await sign({ alg: "RS256" }, { fhirUser: "Practitioner/x", exp: future })), "");
});

// 서명 — 다른 키로 서명한 토큰
await t("타 키 서명 거부", async () => {
  const other = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true, ["sign"]);
  const tok = await sign(H, { iss: ISS, aud: CLIENT_ID, exp: future, fhirUser: "Practitioner/x" }, other.privateKey);
  assert.strictEqual(await verify(tok), "");
});

// 클레임
await t("iss 불일치 거부", async () => {
  assert.strictEqual(await verify(await sign(H, { iss: "https://evil", aud: CLIENT_ID, exp: future, fhirUser: "Practitioner/x" })), "");
});
await t("aud 불일치 거부", async () => {
  assert.strictEqual(await verify(await sign(H, { iss: ISS, aud: "other", exp: future, fhirUser: "Practitioner/x" })), "");
});
await t("만료 토큰 거부", async () => {
  assert.strictEqual(await verify(await sign(H, { iss: ISS, aud: CLIENT_ID, exp: past, fhirUser: "Practitioner/x" })), "");
});

// fhirUser 엄격 파싱 — 폴백 없음
await t("fhirUser=Patient/x 거부 (Practitioner 만)", async () => {
  assert.strictEqual(await verify(await sign(H, { iss: ISS, aud: CLIENT_ID, exp: future, fhirUser: "Patient/x" })), "");
});
await t("fhirUser 없으면 거부", async () => {
  assert.strictEqual(await verify(await sign(H, { iss: ISS, aud: CLIENT_ID, exp: future })), "");
});

console.log(`\n${n} passed`);
