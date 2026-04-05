import type { NextConfig } from "next";

const devDomain = process.env.REPLIT_DEV_DOMAIN || "";

const MCP_ORIGIN = "http://localhost:8001";

const nextConfig: NextConfig = {
  allowedDevOrigins: devDomain ? [devDomain] : [],

  async rewrites() {
    return [
      // MCP streamable-http endpoint → clinical intelligence server
      { source: "/mcp",           destination: `${MCP_ORIGIN}/mcp` },
      { source: "/mcp/:path*",    destination: `${MCP_ORIGIN}/mcp/:path*` },
      // REST tool endpoints
      { source: "/tools/:path*",  destination: `${MCP_ORIGIN}/tools/:path*` },
      // Health check
      { source: "/health",        destination: `${MCP_ORIGIN}/health` },
    ];
  },
};

export default nextConfig;
