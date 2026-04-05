import type { NextConfig } from "next";

const devDomain = process.env.REPLIT_DEV_DOMAIN || "";

const nextConfig: NextConfig = {
  allowedDevOrigins: devDomain ? [devDomain] : [],
};

export default nextConfig;
