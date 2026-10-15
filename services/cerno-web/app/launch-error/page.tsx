// SMART launch 실패 화면. **자체 로그인 폼을 두지 않는다** — 신원 출처는 HIS 하나다.
// 실패 사유를 사용자에게 그대로 노출하지 않고(내부 정보), 무엇을 해야 하는지만 알린다.

const GUIDANCE: Record<string, string> = {
  smart_no_iss: "진료 시스템(HIS)을 거치지 않고 접근했습니다.",
  smart_untrusted_iss: "허용되지 않은 연동 서버입니다.",
  smart_discovery: "진료 시스템의 인증 설정을 읽지 못했습니다.",
  smart_no_jwks: "진료 시스템의 서명 키 정보를 읽지 못했습니다.",
  smart_state: "인증 요청이 만료되었거나 일치하지 않습니다.",
  smart_untrusted_token: "허용되지 않은 토큰 발급처입니다.",
  smart_token: "인증 토큰 발급에 실패했습니다.",
  smart_token_exc: "진료 시스템과 통신하지 못했습니다.",
  smart_idtoken: "신원 확인에 실패했습니다.",
  // IdP 가 authorize 단계에서 돌려준 오류 — callback 이 `smart_${error}` 로 전달한다
  smart_access_denied: "진료 시스템에서 접근이 거부되었습니다 — 권한을 확인해 주세요.",
  smart_invalid_request: "인증 요청 형식이 올바르지 않습니다.",
  smart_server_error: "진료 시스템 인증 서버에 일시적인 문제가 있습니다.",
  smart_temporarily_unavailable: "진료 시스템 인증 서버가 일시적으로 사용 불가합니다.",
};

export default async function LaunchError({
  searchParams,
}: {
  searchParams: Promise<{ error?: string }>;
}) {
  const { error } = await searchParams;
  const message = GUIDANCE[error ?? ""] ?? "인증을 완료하지 못했습니다.";

  return (
    <main style={{
      maxWidth: "34rem", margin: "0 auto", padding: "5rem 1.5rem",
      font: '15px/1.7 -apple-system, "Pretendard", "Apple SD Gothic Neo", system-ui, sans-serif',
    }}>
      <h1 style={{ fontSize: "1.3rem", fontWeight: 650, margin: "0 0 .75rem" }}>
        Cerno에 접속할 수 없습니다
      </h1>
      <p style={{ margin: "0 0 1.25rem", color: "#5b6472" }}>{message}</p>
      <p style={{ margin: 0, color: "#5b6472" }}>
        진료 시스템(HIS) 화면에서 Cerno를 다시 열어 주세요. 문제가 계속되면 담당자에게
        아래 코드를 알려 주십시오.
      </p>
      <code style={{
        display: "inline-block", marginTop: "1rem", padding: ".35rem .7rem",
        background: "#f1f3f7", borderRadius: "6px", fontSize: ".85rem",
      }}>
        {error ?? "unknown"}
      </code>
    </main>
  );
}
