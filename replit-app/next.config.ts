import type { NextConfig } from "next";

const devDomain = process.env.REPLIT_DEV_DOMAIN || "";

const MCP_CLINICAL   = "http://localhost:8001";  // ClinicalIntelligence  — 9 tools
const MCP_SKILLS     = "http://localhost:8002";  // PatientCompanion      — 16 tools
const MCP_INGESTION  = "http://localhost:8003";  // PatientIngestion      — 1 tool

const nextConfig: NextConfig = {
  allowedDevOrigins: devDomain ? [devDomain] : [],

  async rewrites() {
    return [
      // ── Server 1: ClinicalIntelligence (port 8001) ─────────────────────────
      { source: "/mcp",                destination: `${MCP_CLINICAL}/mcp` },
      { source: "/mcp/:path*",         destination: `${MCP_CLINICAL}/mcp/:path*` },
      { source: "/tools/:path*",       destination: `${MCP_CLINICAL}/tools/:path*` },
      { source: "/health",             destination: `${MCP_CLINICAL}/health` },

      // ── Server 2: PatientCompanion / Skills (port 8002) ────────────────────
      { source: "/mcp-skills",         destination: `${MCP_SKILLS}/mcp` },
      { source: "/mcp-skills/:path*",  destination: `${MCP_SKILLS}/mcp/:path*` },

      // ── Server 3: PatientIngestion (port 8003) ─────────────────────────────
      { source: "/mcp-ingestion",      destination: `${MCP_INGESTION}/mcp` },
      { source: "/mcp-ingestion/:path*", destination: `${MCP_INGESTION}/mcp/:path*` },
    ];
  },
};

export default nextConfig;
