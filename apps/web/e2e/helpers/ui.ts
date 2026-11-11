import { expect, type Locator, type Page } from "@playwright/test";

/** The run's status badge next to the page heading (payout rows have badges of their own). */
export const runBadge = (p: Page, label: string): Locator =>
  p
    .getByRole("heading", { level: 1, name: /^Pay run / })
    .locator("xpath=..")
    .getByText(new RegExp(`^\\S\\s*${label}$`));

/** A table row (`<tr>`) containing `text`. */
export const rowWith = (scope: Page | Locator, text: string | RegExp): Locator =>
  scope.getByRole("row").filter({ hasText: text });

/**
 * Pages through a `<CursorPager>` list until a row with `text` is visible (C8) and returns it.
 * Starts from the current page; fails when "Next" is disabled and the row never appeared.
 */
export async function findRow(page: Page, text: string | RegExp): Promise<Locator> {
  const next = page.getByRole("navigation", { name: "Pagination" }).getByRole("button", {
    name: "Next",
  });
  await expect(page.getByRole("table").first()).toBeVisible();
  for (let i = 0; i < 100; i++) {
    const row = rowWith(page, text);
    if ((await row.count()) > 0) return row.first();
    if (!(await next.isEnabled())) break;
    const first = await page.getByRole("row").nth(1).textContent();
    await next.click();
    await expect(page.getByRole("row").nth(1)).not.toHaveText(first ?? "");
  }
  throw new Error(`no row with ${String(text)} on any page`);
}

/** A `role="alert"` with `text` (Next's empty route announcer is an alert too). */
export const alertWith = (scope: Page | Locator, text: string | RegExp): Locator =>
  scope.getByRole("alert").filter({ hasText: text });

/** A `role="status"` with `text` (the run page also has its polite live region). */
export const statusWith = (scope: Page | Locator, text: string | RegExp): Locator =>
  scope.getByRole("status").filter({ hasText: text });
