/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // cerno-api 는 컨테이너 내부에서만 접근한다 — 브라우저에 노출하지 않는다.
  output: "standalone",
  poweredByHeader: false,
  async headers() {
    return [{
      source: "/:path*",
      headers: [
        { key: "X-Content-Type-Options", value: "nosniff" },
        { key: "Referrer-Policy", value: "no-referrer" },
        // EMR iframe 임베드는 규제 범위 결정 후 — 그때 frame-ancestors 를 HIS 로 연다.
        { key: "X-Frame-Options", value: "DENY" },
      ],
    }];
  },
};
module.exports = nextConfig;
