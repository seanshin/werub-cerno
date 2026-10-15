// per-user SMART 토큰 저장소 (Redis).
//
// 01_구현계획.md §3.2 불변 규칙 9 · §9 리스크 #9:
// twin 은 살아있는 HIS access_token 을 **무인증 Redis 에 평문 저장**하고 있었다(v1.20.66 에서
// requirepass 로 봉합). 컨테이너 내 탈취·IDOR 우회로 직결되므로 인증을 강제한다.
//
// 미설정 시 **기동 실패**시킨다 — 조용히 무인증으로 도는 것보다 낫다.
import { createClient, type RedisClientType } from "redis";

export type SmartToken = { access_token: string; patient: string; iss: string };

function url(): string {
  const u = process.env.CERNO_REDIS_URL;
  if (!u) throw new Error("CERNO_REDIS_URL is required");
  // 인증 강제 — redis://[user]:pass@host 형태여야 한다
  if (!/^rediss?:\/\/[^@/]*:[^@/]+@/.test(u)) {
    throw new Error("CERNO_REDIS_URL must include a password (requirepass). 무인증 저장소에 살아있는 HIS 토큰을 두지 않는다.");
  }
  return u;
}

let _client: RedisClientType | null = null;

async function client(): Promise<RedisClientType> {
  if (_client?.isOpen) return _client;
  _client = createClient({ url: url() });
  _client.on("error", () => { /* 재연결은 라이브러리가 처리. PHI 미포함이라 로깅 생략 */ });
  await _client.connect();
  return _client;
}

const key = (uid: string) => `cerno:smart:tok:${uid}`;

export async function putSmartToken(uid: string, tok: SmartToken, ttlSeconds: number): Promise<void> {
  const c = await client();
  await c.set(key(uid), JSON.stringify(tok), { EX: ttlSeconds });
}

/** 없거나 만료면 null → 호출측은 "SMART 재진입 필요"로 degrade 한다. */
export async function getSmartToken(uid: string): Promise<SmartToken | null> {
  const c = await client();
  const v = await c.get(key(uid));
  if (!v) return null;
  try { return JSON.parse(v) as SmartToken; } catch { return null; }
}

export async function delSmartToken(uid: string): Promise<void> {
  const c = await client();
  await c.del(key(uid));
}
