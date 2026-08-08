import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "VidAgent — 视频采集与多模态总结",
  description: "自然语言驱动的视频检索、下载与 AI 总结助手",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN">
      <body className="antialiased">{children}</body>
    </html>
  );
}
