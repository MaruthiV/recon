// readme screenshots from the real local stack (recon dev must be up), run: node scripts/screenshots.mjs [names]
import { execFileSync } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
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

const password = execFileSync(
  "uv",
  ["run", "python", "-c", "from recon_storage.users import demo_password; print(demo_password())"],
  { cwd: new URL("../../../", import.meta.url).pathname, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
).trim();

async function login(page, role, tenant = "t_acme") {
  await page.goto(`${base}/login`);
  await page.getByLabel("Name").fill(role);
  await page.getByLabel("Password").fill(password);
  await page.getByLabel("Tenant").fill(tenant);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL(`${base}/`);
}

// no names means all of them
const only = new Set(process.argv.slice(2));
const want = (name) => !only.size || only.has(name);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 });
// next's dev badge isn't part of the app
const shot = async (name, clip) => {
  await page.addStyleTag({ content: "nextjs-portal{display:none!important}" });
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: !clip, clip });
};

// 1. the worker dies right after the provider commits, the case still ends with one refund
if (want("crash-recovery")) {
  const crash = seed();
  await login(page, "operator");
  await page.goto(`${base}/cases/${crash}`);
  await page.getByRole("button", { name: /Kill the worker after the provider commits/ }).click();
  await page.getByRole("button", { name: "Investigate (rules, no model)" }).click();
  await page.getByText("CLOSED · REFUNDED").first().waitFor({ timeout: 120_000 });
  await page.waitForTimeout(1500);
  await shot("crash-recovery");
}

// 2. over the limit: the plan waits for a supervisor
if (want("approval")) {
  const big = seed(25_000);
  await login(page, "supervisor");
  await page.goto(`${base}/cases/${big}`);
  await page.getByRole("button", { name: "Investigate (rules, no model)" }).click();
  await page.getByRole("button", { name: "Approve this refund" }).waitFor({ timeout: 60_000 });
  await page.waitForTimeout(1000);
  await shot("approval");
}

// 3. the inbox with every workflow seeded (recon dev --families), each one run through rules first
if (want("inbox")) {
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
}

// 4. every approach on the validation split, read from results/eval
if (want("benchmarks")) {
  await login(page, "operator");
  await page.goto(`${base}/benchmarks`);
  await page.getByRole("button", { name: /rules: \d+ misses/ }).waitFor({ timeout: 30_000 });
  await page.waitForTimeout(1000);
  await shot("benchmarks");
}

// 5. the same crash as a gif: a frame every half second from the click to the close, stitched with imagemagick
if (want("demo")) {
  const id = seed();
  await login(page, "operator");
  await page.goto(`${base}/cases/${id}`);
  await page.addStyleTag({ content: "nextjs-portal{display:none!important}" });
  const frames = mkdtempSync(join(tmpdir(), "recon-demo-"));
  let n = 0;
  const frame = async () => page.screenshot({ path: join(frames, `${String(n++).padStart(3, "0")}.png`) });
  await frame();
  await frame();
  await page.getByRole("button", { name: /Kill the worker after the provider commits/ }).click();
  await frame();
  await page.getByRole("button", { name: "Investigate (rules, no model)" }).click();
  const closed = page.getByText("CLOSED · REFUNDED").first();
  for (let i = 0; i < 120 && !(await closed.isVisible()); i++) {
    await frame();
    await page.waitForTimeout(500);
  }
  await page.waitForTimeout(800);
  for (let i = 0; i < 6; i++) await frame(); // hold on the end state
  execFileSync("magick", [
    "-delay", "50", "-loop", "0", join(frames, "*.png"),
    "-resize", "1100x", "-colors", "96", "-layers", "Optimize", `${out}/crash-recovery.gif`,
  ]);
  rmSync(frames, { recursive: true });
}

await browser.close();
console.log("wrote", out);
