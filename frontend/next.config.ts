import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // FastAPI 后端地址（开发时本机，部署时 AutoDL 公网 URL）
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000",
  },
};

export default nextConfig;
