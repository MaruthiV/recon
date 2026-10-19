import { expect, test } from "@playwright/test";
import { commands, login, seed } from "./helpers";

test("an over-limit refund waits for a supervisor, then moves once", async ({ page }) => {
  const id = seed(25_000);

  await login(page, "operator");
  await page.goto(`/cases/${id}`);
  await page.getByRole("button", { name: "Investigate (rules, no model)" }).click();
  await expect(page.getByText("AWAITING_APPROVAL").first()).toBeVisible({ timeout: 60_000 });
  // an operator sees the plan but can't approve it
  await expect(page.getByText("Waiting for a supervisor to approve.")).toBeVisible();
  await expect(page.getByRole("button", { name: "Approve this refund" })).toHaveCount(0);
  expect(await commands(page, id)).toHaveLength(0);

  await login(page, "supervisor");
  await page.goto(`/cases/${id}`);
  await page.getByRole("button", { name: "Approve this refund" }).click();
  await expect(page.getByText("CLOSED · REFUNDED").first()).toBeVisible({ timeout: 60_000 });

  const cmds = await commands(page, id);
  expect(cmds).toHaveLength(1);
  expect(cmds[0]).toMatchObject({ state: "CONFIRMED_SUCCESS", attempts: 1, amount_minor: 25_000 });
});
