import type { NextConfig } from "next";

const api = process.env.RECON_API_URL ?? "http://127.0.0.1:8000";

// same-origin proxy so the session cookie just works
const config: NextConfig = {
  // repo rule: no markdown outside docs/, so no generated AGENTS.md / CLAUDE.md
  agentRules: false,
  async rewrites() {
    return [
      { source: "/api/v1/:path*", destination: `${api}/v1/:path*` },
      { source: "/api/internal/demo/:path*", destination: `${api}/internal/demo/:path*` },
    ];
  },
};

export default config;
