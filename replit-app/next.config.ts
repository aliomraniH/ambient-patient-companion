import type { NextConfig } from "next";

const devDomain = process.env.REPLIT_DEV_DOMAIN || "";

const MCP_CLINICAL   = "http://localhost:8001";  // ambient-clinical-intelligence — 19 tools
const MCP_SKILLS     = "http://localhost:8002";  // ambient-skills-companion     — 18 tools
const MCP_INGESTION  = "http://localhost:8003";  // ambient-ingestion            — 1 tool

const nextConfig: NextConfig = {
  allowedDevOrigins: devDomain ? [devDomain] : [],

  async rewrites() {
    return [
      // ── Server 1: ambient-clinical-intelligence (port 8001) ────────────────
      { source: "/mcp",                destination: `${MCP_CLINICAL}/mcp` },
      { source: "/mcp/:path*",         destination: `${MCP_CLINICAL}/mcp/:path*` },
      { source: "/tools/:path*",       destination: `${MCP_CLINICAL}/tools/:path*` },
      { source: "/health",             destination: `${MCP_CLINICAL}/health` },

      // ── Server 2: ambient-skills-companion (port 8002) ─────────────────────
      { source: "/mcp-skills",         destination: `${MCP_SKILLS}/mcp` },
      { source: "/mcp-skills/:path*",  destination: `${MCP_SKILLS}/mcp/:path*` },

      // ── Server 3: ambient-ingestion (port 8003) ────────────────────────────
      { source: "/mcp-ingestion",      destination: `${MCP_INGESTION}/mcp` },
      { source: "/mcp-ingestion/:path*", destination: `${MCP_INGESTION}/mcp/:path*` },
    ];
  },
};

export default nextConfig;
