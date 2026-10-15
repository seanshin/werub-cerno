import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Cerno",
  description: "의료진별 개인화 임상 지식 AI — 근거 기반 보조 도구",
  // 원내 내부 도구다. 검색 노출·미리보기 생성을 막는다.
  robots: { index: false, follow: false },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ko">
      <body>
        {children}
        <footer className="cerno-disclaimer">
          Cerno는 의료기기가 아니며 진단·처방을 자동 확정하지 않습니다. 최종 판단은 의료진에게 있습니다.
        </footer>
      </body>
    </html>
  );
}
