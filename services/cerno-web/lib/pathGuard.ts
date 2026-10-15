// 경로 traversal 방어 (보안 크리티컬) — Next 런타임 비의존 순수 함수라 단위 테스트 가능.
//
// 이식 원본: twin-web/lib/pathGuard.ts
// 배경: twin 에서 인코딩된 `..%2F` 가 프록시의 path.join+fetch 정규화를 유도해
//       **인증 없이 PHI 라우트에 도달**한 실사례가 있었다(라이브 확증 Critical).
//       middleware 1차 + 프록시 2차의 이중 게이트로 막는다(불변 규칙 6·8).

/** pathname(인코딩 가능)에 상위경로/역슬래시 성분이 있으면 true. */
export function hasTraversal(rawPath: string): boolean {
  let decoded = rawPath;
  for (let i = 0; i < 3; i++) {          // 다중 인코딩(%252F=%2F 등) 방어
    try {
      const d = decodeURIComponent(decoded);
      if (d === decoded) break;
      decoded = d;
    } catch {
      break;
    }
  }
  return (
    rawPath.includes("..") ||
    decoded.includes("..") ||
    /%2e/i.test(rawPath) ||
    /%2f/i.test(rawPath) ||
    /%5c/i.test(rawPath) ||
    decoded.includes("\\")
  );
}

/** 프록시 catch-all 세그먼트 배열에 상위경로/빈 성분이 있으면 true(심층방어 2차 게이트). */
export function badSegments(path: string[]): boolean {
  return path.some((s) => s === ".." || s === "." || s === "" || s.includes("\\"));
}
