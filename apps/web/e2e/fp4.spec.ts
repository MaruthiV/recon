import { expect, test } from "@playwright/test";
import { commands, login, seed } from "./helpers";

test("killing the worker right after the provider commits still ends in one refund", async ({ page }) => {
  const id = seed();

  await login(page, "operator");
  await page.goto(`/cases/${id}`);
  await page.getByRole("button", { name: /Kill the worker after the provider commits/ }).click();
  await expect(page.getByText(/Armed: the next worker/)).toBeVisible();
  await page.getByRole("button", { name: "Investigate (rules, no model)" }).click();

  // the dev supervisor restarts the dead worker, the sweeper re-enqueues, reconcile finds the refund
  await expect(page.getByText("CLOSED · REFUNDED").first()).toBeVisible({ timeout: 120_000 });

  const cmds = await commands(page, id);
  expect(cmds).toHaveLength(1);
  // sent once before the crash, recovered by reading the provider, never sent again
  expect(cmds[0]).toMatchObject({ state: "CONFIRMED_SUCCESS", attempts: 1 });
});
