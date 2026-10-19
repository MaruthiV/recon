// readme screenshots from the real local stack (recon dev must be up), run: node scripts/screenshots.mjs
import { execFileSync } from "node:child_process";
import { chromium } from "@playwright/test";

const out = new URL("../../../assets/", import.meta.url).pathname;
const base = "http://localhost:3000";

function seed(amount = 4900) {
  const text = execFileSync("uv", ["run", "recon", "seed", "--amount", String(amount)], {
    cwd: new URL("../../../", import.meta.url).pathname,
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  return JSON.parse(text.trim().split("\n").at(-1)).case_id;
}

async function login(page, role, tenant = "t_acme") {
  await page.goto(`${base}/login`);
  await page.getByLabel("Name").fill("maruthi");
  await page.getByLabel("Tenant").fill(tenant);
  await page.getByLabel("Role").selectOption(role);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${base}/`);
}

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
// next's dev badge isn't part of the app
const shot = async (name, clip) => {
  await page.addStyleTag({ content: "nextjs-portal{display:none!important}" });
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: !clip, clip });
};

// 1. the worker dies right after the provider commits, the case still ends with one refund
const crash = seed();
await login(page, "operator");
await page.goto(`${base}/cases/${crash}`);
await page.getByRole("button", { name: /Kill the worker after the provider commits/ }).click();
await page.getByRole("button", { name: "Investigate (rules, no model)" }).click();
await page.getByText("CLOSED · REFUNDED").first().waitFor({ timeout: 120_000 });
await page.waitForTimeout(1500);
await shot("crash-recovery");

// 2. over the limit: the plan waits for a supervisor
const big = seed(25_000);
await login(page, "supervisor");
await page.goto(`${base}/cases/${big}`);
await page.getByRole("button", { name: "Investigate (rules, no model)" }).click();
await page.getByRole("button", { name: "Approve this refund" }).waitFor({ timeout: 60_000 });
await page.waitForTimeout(1000);
await shot("approval");

// 3. the inbox with every workflow seeded (recon dev --families), each one run through rules first
await login(page, "operator", "t_demo_acme");
const cases = await (await page.request.get(`${base}/api/v1/cases`)).json();
for (const c of cases.filter((c) => c.state === "OPEN")) {
  await page.request.post(`${base}/api/v1/cases/${c.id}/investigations`, {
    data: { mode: "rules", case_version: 1 },
    headers: { "Idempotency-Key": `shot-${c.id}` },
  });
}
await page.waitForTimeout(15_000);
await page.reload();
await page.waitForTimeout(1000);
const box = await page.locator(".panel").first().boundingBox();
await shot("inbox", { x: 0, y: 0, width: 1360, height: Math.ceil((box?.y ?? 0) + (box?.height ?? 600) + 40) });

await browser.close();
console.log("wrote", out);
