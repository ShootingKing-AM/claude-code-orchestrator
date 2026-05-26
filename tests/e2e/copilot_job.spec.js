// @ts-check
/**
 * E2E tests for GitHub Copilot backend jobs.
 *
 * Prerequisites:
 *   - Server running: make start   (port 8888)
 *   - COPILOT_TOKEN set in .env (gho_... OAuth token)
 *
 * Run: npx playwright test tests/e2e/copilot_job.spec.js --headed
 */

const { test, expect } = require("@playwright/test");

const BASE = "http://localhost:8888";
const COPILOT_TIMEOUT = 90_000; // API can be slow

// ── Helpers ───────────────────────────────────────────────────────────────────

async function submitCopilotJob(page, prompt, { model = null } = {}) {
  await page.click("#new-job-btn");
  await page.waitForSelector("#modal-overlay.open");
  await page.fill("#prompt-input", prompt);

  // Select Copilot backend
  await page.selectOption("#backend-select", "copilot");

  if (model) {
    await page.fill("#model-input", model);
  }

  await page.click("#modal-submit");
  await page.waitForSelector("#output-wrap", { state: "visible", timeout: 5_000 });
}

async function waitForJobState(page, state, timeout = COPILOT_TIMEOUT) {
  await page.waitForFunction(
    (s) => {
      const el = document.querySelector("#meta-state .badge");
      return el && el.textContent.trim().toLowerCase().includes(s);
    },
    state,
    { timeout }
  );
}

async function getDetailText(page) {
  return page.locator("#output-wrap").innerText();
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe("Copilot backend", () => {
  // Copilot API can be slow — override global 30s timeout for this whole suite
  test.setTimeout(120_000);
  test("backends endpoint reports copilot available", async ({ request }) => {
    const resp = await request.get(`${BASE}/api/backends`);
    expect(resp.ok()).toBeTruthy();
    const body = await resp.json();
    expect(body.backends).toContain("copilot");
    expect(body.copilot_available).toBe(true);
  });

  test("job creation modal has backend dropdown", async ({ page }) => {
    await page.goto(BASE);
    await page.click("#new-job-btn");
    await page.waitForSelector("#modal-overlay.open");

    const select = page.locator("#backend-select");
    await expect(select).toBeVisible();

    const options = await select.locator("option").allTextContents();
    expect(options.some((o) => o.toLowerCase().includes("claude"))).toBe(true);
    expect(options.some((o) => o.toLowerCase().includes("copilot"))).toBe(true);

    await page.screenshot({ path: "test-results/copilot-modal.png", fullPage: true });
  });

  test("copilot job completes and shows [Copilot] badge", async ({ page }) => {
    await page.goto(BASE);

    await submitCopilotJob(page, "Reply with exactly: COPILOT_OK");

    // Job card should show Copilot badge
    const jobCard = page.locator(".job-item").first();
    await expect(jobCard.locator(".badge-backend-copilot")).toBeVisible({ timeout: 5_000 });

    await page.screenshot({ path: "test-results/copilot-running.png", fullPage: true });

    // Wait for completion — UI renders 'completed' as 'done'
    await waitForJobState(page, "done");

    await page.screenshot({ path: "test-results/copilot-completed.png", fullPage: true });

    // Verify the response contains expected text
    const text = await getDetailText(page);
    expect(text.toLowerCase()).toContain("copilot_ok");
  });

  test("copilot job failure shows error details", async ({ page }) => {
    // Submit with an invalid model name to force a failure
    // #model-input is a <select>, so we pick an option then override via API
    await page.goto(BASE);

    // Directly POST a job with a bogus model via the API
    const resp = await page.request.post(`${BASE}/api/jobs`, {
      data: { prompt: "say hello", backend: "copilot", model: "nonexistent-model-xyz" },
    });
    expect(resp.ok()).toBeTruthy();
    const job = await resp.json();

    // Navigate to show the job
    await page.goto(BASE);
    // Click first job in list
    await page.locator(".job-item").first().click();
    await page.waitForSelector("#output-wrap", { state: "visible", timeout: 5_000 });

    // Should fail (bad model name)
    await waitForJobState(page, "failed", 30_000);

    await page.screenshot({ path: "test-results/copilot-failed.png", fullPage: true });

    // Detail panel should show some error text
    const text = await getDetailText(page);
    expect(text.length).toBeGreaterThan(0);
  });

  test("job detail panel shows Copilot backend badge", async ({ page }) => {
    await page.goto(BASE);

    await submitCopilotJob(page, "say hi briefly");

    // Wait for state to settle (running or completed)
    await page.waitForFunction(
      () => {
        const el = document.querySelector("#meta-state .badge");
        return el && ["running", "completed", "failed"].some((s) =>
          el.textContent.trim().toLowerCase().includes(s)
        );
      },
      null,
      { timeout: 10_000 }
    );

    // The detail panel should show a backend badge (use the specific ID to avoid strict mode violation)
    const badge = page.locator("#meta-backend-badge");
    await expect(badge).toBeVisible({ timeout: 5_000 });
    const badgeText = await badge.innerText();
    expect(badgeText.toLowerCase()).toContain("copilot");

    await page.screenshot({ path: "test-results/copilot-detail-badge.png", fullPage: true });
  });

  test("screenshot: copilot job completed state", async ({ page }) => {
    await page.goto(BASE);
    await submitCopilotJob(page, "What is 2+2? Answer with just the number.");
    await waitForJobState(page, "done");
    await page.waitForLoadState("networkidle");
    await page.screenshot({ path: "test-results/copilot-final-state.png", fullPage: true });
  });
});
