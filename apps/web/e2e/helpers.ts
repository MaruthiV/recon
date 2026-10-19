import { execFileSync } from "node:child_process";
import { expect, type Page } from "@playwright/test";

// a fresh duplicate-charge case for tenant t_acme, through the same cli a developer uses
export function seed(amount = 4900): string {
  const out = execFileSync("uv", ["run", "recon", "seed", "--amount", String(amount)], {
    cwd: "../..",
    encoding: "utf8",
    stdio: ["ignore", "pipe", "ignore"],
  });
  return JSON.parse(out.trim().split("\n").at(-1) ?? "{}").case_id;
}

export async function login(page: Page, role: "operator" | "supervisor") {
  await page.goto("/login");
  await page.getByLabel("Role").selectOption(role);
  await page.getByRole("button", { name: "Sign in" }).click();
  await page.waitForURL("/");
}

// recon's own record of what went to the provider for this case
export async function commands(page: Page, id: string) {
  const r = await page.request.get(`/api/v1/cases/${id}`);
  expect(r.ok()).toBeTruthy();
  return (await r.json()).commands as { state: string; attempts: number; amount_minor: number }[];
}
