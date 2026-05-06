import type { NextConfig } from "next";

// Support both Replit and Vercel deployments
const devDomain = process.env.REPLIT_DEV_DOMAIN || process.env.VERCEL_URL || "";

// MCP Server URLs - use environment variables for external deployment, fallback to localhost for local dev
const MCP_CLINICAL   = process.env.MCP_CLINICAL_URL   || "http://localhost:8001";  // ambient-clinical-intelligence — 19 tools
const MCP_SKILLS     = process.env.MCP_SKILLS_URL     || "http://localhost:8002";  // ambient-skills-companion     — 18 tools
const MCP_INGESTION  = process.env.MCP_INGESTION_URL  || "http://localhost:8003";  // ambient-ingestion            — 1 tool

const nextConfig: NextConfig = {
  allowedDevOrigins: devDomain ? [devDomain] : [],

  async rewrites() {
    // Only set up rewrites if MCP servers are configured (local dev or self-hosted)
    // For Vercel deployment without MCP servers, these endpoints won't be available
    const rewrites = [];

    if (MCP_CLINICAL) {
      rewrites.push(
        { source: "/mcp",                destination: `${MCP_CLINICAL}/mcp` },
        { source: "/mcp/:path*",         destination: `${MCP_CLINICAL}/mcp/:path*` },
        { source: "/tools/:path*",       destination: `${MCP_CLINICAL}/tools/:path*` },
        { source: "/health",             destination: `${MCP_CLINICAL}/health` },
      );
    }

    if (MCP_SKILLS) {
      rewrites.push(
        { source: "/mcp-skills",         destination: `${MCP_SKILLS}/mcp` },
        { source: "/mcp-skills/:path*",  destination: `${MCP_SKILLS}/:path*` },
      );
    }

    if (MCP_INGESTION) {
      rewrites.push(
        { source: "/mcp-ingestion",      destination: `${MCP_INGESTION}/mcp` },
        { source: "/mcp-ingestion/:path*", destination: `${MCP_INGESTION}/:path*` },
      );
    }

    return rewrites;
  },
};

export default nextConfig;
