import { defineConfig } from "@playwright/test";

// drives the real local stack (sim, mcp, worker, api, web), no model calls
export default defineConfig({
  testDir: "e2e",
  timeout: 180_000,
  workers: 1,
  reporter: [["list"]],
  use: { baseURL: "http://localhost:3000", trace: "retain-on-failure" },
  webServer: {
    command: "make db >/dev/null && uv run recon dev",
    cwd: "../..",
    url: "http://localhost:3000/login",
    timeout: 240_000,
    reuseExistingServer: true,
    // sigterm first so recon dev can stop its children, next included
    gracefulShutdown: { signal: "SIGTERM", timeout: 15_000 },
  },
});
