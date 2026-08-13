import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // FastAPI 后端地址（开发时本机，部署时 AutoDL 公网 URL）
  env: {
    NEXT_PUBLIC_API_URL: process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000",
  },
  // Docker 部署：产出自包含 server（仅需最小 node 运行时）
  output: "standalone",
};

export default nextConfig;
