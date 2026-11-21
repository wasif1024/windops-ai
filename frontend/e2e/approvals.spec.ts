// The approval queue in a real browser, through the gateway (M10.2). The frontend check makes a
// Maintenance Manager and a submitted work order for each screen size. The manager finds its
// request in the queue, reviews the plan, approves it, and sees the decision recorded with the
// rules it was held to. Nothing may scroll sideways (UX-001).
import { expect, test } from "@playwright/test";
import { noSidewaysScroll, openScreen, signIn } from "./helpers";

const tenant = process.env.TENANT ?? "north-sea";
const password = process.env.PLANNER_PASSWORD ?? "";

function setting(name: string): string {
  return process.env[`${name}_${test.info().project.name.toUpperCase()}`] ?? "";
}

test("a manager reviews a submitted work order and approves it", async ({ page, isMobile }) => {
  const [email, workOrder] = [setting("MANAGER_EMAIL"), setting("MANAGER_WORK_ORDER")];
  expect(email && password && workOrder, "make frontend-check passes the manager and work order").toBeTruthy();
  await page.goto(`/?tenant=${tenant}`);
  await signIn(page, email, password);

  await openScreen(page, isMobile, "Approval queue");
  const row = page.getByRole("link", { name: new RegExp(workOrder) });
  await expect(row).toContainText("Pending");
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("approval-queue.png"), fullPage: true });
  await row.click();

  // What is to be decided: the plan, its validation and its history.
  await expect(page.getByRole("link", { name: "← Approval queue" })).toBeVisible();
  await expect(page.getByTestId("validation")).toContainText("Nothing blocks it.", { timeout: 30_000 });
  await expect(page.getByRole("list", { name: "History" })).toContainText("Submitted");
  await noSidewaysScroll(page);
  await page.screenshot({ path: test.info().outputPath("approval.png"), fullPage: true });

  await page.getByLabel("Reason (needed to reject)").fill("The monitoring plan fits the findings.");
  await page.getByRole("button", { name: "Approve" }).click();
  const decision = page.getByTestId("decision");
  await expect(decision).toContainText("Approved", { timeout: 30_000 });
  await expect(decision).toContainText("separation-of-duties-1");
  await expect(page.getByRole("button", { name: "Approve" })).toHaveCount(0);
  await noSidewaysScroll(page);
});
