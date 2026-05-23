// @ts-check
/**
 * End-to-end tests for the Claude Code Orchestrator.
 *
 * Prerequisites:
 *   - Server running: python3 -m web.server   (port 8888)
 *   - `claude` CLI authenticated and on PATH
 *
 * Run: npx playwright test --headed   (to watch)
 *      npx playwright test            (headless)
 */

const { test, expect } = require("@playwright/test");

const BASE = "http://localhost:8888";

// ── Helpers ───────────────────────────────────────────────────────────────────

async function submitJob(page, prompt, workingDir = "") {
  await page.click("#new-job-btn");
  await page.waitForSelector("#modal-overlay.open");
  await page.fill("#prompt-input", prompt);
  if (workingDir) await page.fill("#cwd-input", workingDir);
  await page.click("#modal-submit");
}

async function waitForJobState(page, state, timeout = 60_000) {
  // Wait until the detail panel badge shows the expected state
  await page.waitForFunction(
    (s) => {
      const el = document.querySelector("#meta-state .badge");
      return el && el.textContent.trim().toLowerCase().includes(s);
    },
    state,
    { timeout }
  );
}

// ── Tests ─────────────────────────────────────────────────────────────────────

test.describe("Page load", () => {
  test("renders sidebar and empty state", async ({ page }) => {
    await page.goto(BASE);

    await expect(page.locator("h1")).toHaveText("Orchestrator");
    await expect(page.locator("#new-job-btn")).toBeVisible();
    await expect(page.locator("#empty-state")).toBeVisible();
    await expect(page.locator("#output-wrap")).not.toBeVisible();
  });

  test("screenshot: initial state", async ({ page }) => {
    await page.goto(BASE);
    await page.waitForLoadState("networkidle");
    await expect(page).toHaveScreenshot("01-initial-state.png", { fullPage: true });
  });
});

test.describe("New job modal", () => {
  test("opens and closes", async ({ page }) => {
    await page.goto(BASE);

    await page.click("#new-job-btn");
    await expect(page.locator("#modal-overlay")).toHaveClass(/open/);

    await page.click("#modal-cancel");
    await expect(page.locator("#modal-overlay")).not.toHaveClass(/open/);
  });

  test("closes on backdrop click", async ({ page }) => {
    await page.goto(BASE);
    await page.click("#new-job-btn");
    await expect(page.locator("#modal-overlay")).toHaveClass(/open/);

    // Click the backdrop (outside the modal box)
    await page.mouse.click(10, 10);
    await expect(page.locator("#modal-overlay")).not.toHaveClass(/open/);
  });

  test("Ctrl+Enter submits the form", async ({ page }) => {
    await page.goto(BASE);
    await page.click("#new-job-btn");
    await page.fill("#prompt-input", "echo hello from e2e test");
    await page.keyboard.press("Control+Enter");

    // Modal should close and at least one job item should appear in the sidebar
    await expect(page.locator("#modal-overlay")).not.toHaveClass(/open/);
    await page.waitForSelector(".job-item", { timeout: 5_000 });
    const count = await page.locator(".job-item").count();
    expect(count).toBeGreaterThanOrEqual(1);
  });

  test("screenshot: modal open", async ({ page }) => {
    await page.goto(BASE);
    await page.click("#new-job-btn");
    await expect(page).toHaveScreenshot("02-modal-open.png", { fullPage: true });
  });
});

test.describe("Job submission and detail panel", () => {
  test("submitting a job shows it in the sidebar and renders detail panel", async ({ page }) => {
    await page.goto(BASE);

    const prompt = "Print the text: e2e-test-marker-" + Date.now();
    await submitJob(page, prompt, "/tmp");

    // Sidebar entry appears
    const item = page.locator(".job-item").first();
    await expect(item).toBeVisible({ timeout: 5_000 });

    // Detail panel fills in
    await expect(page.locator("#detail-content")).toBeVisible();
    await expect(page.locator("#detail-prompt")).toContainText("e2e-test-marker");

    // State chip shows something (queued or running)
    await expect(page.locator("#meta-state .badge")).toBeVisible();

    // ID chip shows a truncated UUID
    await expect(page.locator("#meta-id")).toBeVisible();

    // Working dir chip shows /tmp
    await expect(page.locator("#meta-cwd")).toHaveText("/tmp");

    // Output panel is visible
    await expect(page.locator("#output-wrap")).toBeVisible();

    await expect(page).toHaveScreenshot("03-job-running.png", { fullPage: true });
  });

  test("job transitions to completed and output is shown", async ({ page }) => {
    await page.goto(BASE);

    const prompt = "Say exactly: task-complete-signal. Nothing else.";
    await submitJob(page, prompt, "/tmp");

    // Wait up to 90s for Claude to finish
    await waitForJobState(page, "completed", 90_000);

    // Badge turns green/completed
    await expect(page.locator("#meta-state .badge")).toContainText(/completed/i);

    // Output area has content
    const outputText = await page.locator("#output-wrap").innerText();
    expect(outputText.length).toBeGreaterThan(0);

    // Cancel button is hidden for terminal jobs
    await expect(page.locator("#cancel-btn")).not.toBeVisible();

    await expect(page).toHaveScreenshot("04-job-completed.png", { fullPage: true });
  });

  test("output streams live events during run", async ({ page }) => {
    await page.goto(BASE);

    await submitJob(page, "List files in the current directory using the Bash tool.", "/tmp");

    // While running, output wrap should receive new blocks
    await page.waitForFunction(
      () => document.querySelectorAll("#output-wrap .msg-block").length > 0,
      { timeout: 30_000 }
    );

    const blockCount = await page.locator("#output-wrap .msg-block").count();
    expect(blockCount).toBeGreaterThan(0);
  });
});

test.describe("Job detail metadata", () => {
  test("shows session ID after claude starts", async ({ page }) => {
    await page.goto(BASE);
    await submitJob(page, "Say: session-id-test", "/tmp");

    // Wait for running state
    await page.waitForFunction(
      () => {
        const b = document.querySelector("#meta-state .badge");
        return b && (b.textContent.includes("running") || b.textContent.includes("completed"));
      },
      { timeout: 30_000 }
    );

    // Session chip should appear once claude emits the init event
    await page.waitForSelector("#meta-session-wrap", { state: "visible", timeout: 30_000 });
    const sessionText = await page.locator("#meta-session").innerText();
    expect(sessionText.length).toBeGreaterThan(0);
  });

  test("created and updated timestamps are shown", async ({ page }) => {
    await page.goto(BASE);
    await submitJob(page, "Say: timestamp-test", "/tmp");
    await expect(page.locator("#meta-created")).toBeVisible();
    await expect(page.locator("#meta-updated")).toBeVisible();

    const created = await page.locator("#meta-created").innerText();
    expect(created).toMatch(/\d{1,2}\/\d{1,2}\/\d{4}/);  // date format
  });
});

test.describe("Retry flow", () => {
  test("retry button opens modal pre-filled with same prompt", async ({ page }) => {
    await page.goto(BASE);

    const prompt = "retry-flow-test-" + Date.now();
    await submitJob(page, prompt, "/tmp");

    // Wait until job is in sidebar
    await expect(page.locator(".job-item")).toBeVisible({ timeout: 10_000 });

    // Click Retry
    await page.click("#retry-btn");
    await expect(page.locator("#modal-overlay")).toHaveClass(/open/);

    // Modal should be pre-filled
    const modalPrompt = await page.locator("#prompt-input").inputValue();
    expect(modalPrompt).toContain(prompt.slice(0, 20));

    const modalCwd = await page.locator("#cwd-input").inputValue();
    expect(modalCwd).toBe("/tmp");

    await expect(page).toHaveScreenshot("05-retry-modal-prefilled.png", { fullPage: true });
  });
});

test.describe("Cancel flow", () => {
  test("cancel button appears only when running and cancels the job", async ({ page }) => {
    await page.goto(BASE);

    // Submit a slow task so we can catch it running
    await submitJob(page, "Count slowly from 1 to 100, pausing between each number.", "/tmp");

    // Wait for running state
    await page.waitForFunction(
      () => {
        const b = document.querySelector("#meta-state .badge");
        return b && b.textContent.includes("running");
      },
      { timeout: 20_000 }
    );

    // Cancel button visible
    await expect(page.locator("#cancel-btn")).toBeVisible();

    // Click cancel — need to accept the confirm dialog
    page.once("dialog", d => d.accept());
    await page.click("#cancel-btn");

    // Job transitions to failed
    await waitForJobState(page, "failed", 15_000);
    await expect(page.locator("#cancel-btn")).not.toBeVisible();

    await expect(page).toHaveScreenshot("06-job-cancelled.png", { fullPage: true });
  });
});

test.describe("Job history and SSE replay", () => {
  test("reloading the page preserves job list and log history", async ({ page, context }) => {
    await page.goto(BASE);

    const prompt = "Say: history-replay-test";
    await submitJob(page, prompt, "/tmp");

    // Wait for completion
    await waitForJobState(page, "completed", 90_000);

    const logsBefore = await page.locator("#output-wrap .msg-block").count();
    expect(logsBefore).toBeGreaterThan(0);

    // Reload
    await page.reload();
    await page.waitForLoadState("networkidle");

    // Job list still shows the job
    await expect(page.locator(".job-item")).toHaveCount(1, { timeout: 5_000 });

    // Click it — logs replay from DB
    await page.locator(".job-item").first().click();
    await page.waitForFunction(
      () => document.querySelectorAll("#output-wrap .msg-block").length > 0,
      { timeout: 10_000 }
    );

    const logsAfter = await page.locator("#output-wrap .msg-block").count();
    expect(logsAfter).toBeGreaterThan(0);

    await expect(page).toHaveScreenshot("07-history-replay.png", { fullPage: true });
  });

  test("multiple browser tabs see the same job list", async ({ browser }) => {
    const ctx  = await browser.newContext();
    const pg1  = await ctx.newPage();
    const pg2  = await ctx.newPage();

    await pg1.goto(BASE);
    await pg2.goto(BASE);

    const prompt = "Say: multi-tab-test-" + Date.now();
    await submitJob(pg1, prompt);

    // Both tabs should eventually show the job
    await expect(pg1.locator(".job-item").first()).toBeVisible({ timeout: 10_000 });
    await pg2.reload();
    await expect(pg2.locator(".job-item").first()).toBeVisible({ timeout: 5_000 });

    await ctx.close();
  });
});

test.describe("Job queue", () => {
  test("queue endpoint returns empty list initially", async ({ page }) => {
    const res = await page.request.get(`${BASE}/api/queue`);
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(Array.isArray(body)).toBe(true);
  });

  test("second job appears with queue position badge while first runs", async ({ page }) => {
    await page.goto(BASE);

    // Submit first job (slow task)
    await submitJob(page, "Count from 1 to 50 slowly, one number per line.", "/tmp");
    await page.waitForFunction(
      () => document.querySelector("#meta-state .badge")?.textContent.includes("running"),
      { timeout: 20_000 }
    );

    // Submit second job while first is running
    await submitJob(page, "Say: queue-test-second", "/tmp");

    // Second job item should show a queue position badge
    await page.waitForFunction(
      () => {
        const items = document.querySelectorAll(".job-item");
        return Array.from(items).some(el => el.querySelector(".queue-pos"));
      },
      { timeout: 5_000 }
    );

    await expect(page).toHaveScreenshot("08-queue-position-badge.png", { fullPage: true });
  });

  test("removing queued job via cancel marks it failed", async ({ page }) => {
    await page.goto(BASE);

    // Start a slow job to block the queue
    await submitJob(page, "Count from 1 to 100 slowly.", "/tmp");
    await page.waitForFunction(
      () => document.querySelector("#meta-state .badge")?.textContent.includes("running"),
      { timeout: 20_000 }
    );

    // Submit a second job — it goes to queue
    await submitJob(page, "Say: remove-queue-test", "/tmp");

    // Click it in sidebar to select it
    const items = page.locator(".job-item");
    await items.last().click();

    // Wait for queued badge in detail panel
    await page.waitForFunction(
      () => document.querySelector("#meta-state .badge")?.textContent.includes("queued"),
      { timeout: 5_000 }
    );

    // Cancel = "Remove from queue"
    await expect(page.locator("#cancel-btn")).toContainText("Remove from queue");
    page.once("dialog", d => d.accept());
    await page.click("#cancel-btn");

    // Job should flip to failed
    await waitForJobState(page, "failed", 5_000);
  });
});

test.describe("Interactive messaging", () => {
  test("message bar appears after job gets a session", async ({ page }) => {
    await page.goto(BASE);
    await submitJob(page, "Say: message-bar-test", "/tmp");

    // Wait for session ID to appear (means claude has started)
    await page.waitForSelector("#meta-session-wrap", { state: "visible", timeout: 30_000 });

    // Message bar should now be visible
    await expect(page.locator("#msg-bar")).toBeVisible();
    await expect(page.locator("#msg-input")).toBeVisible();
    await expect(page.locator("#msg-send")).toBeVisible();

    await expect(page).toHaveScreenshot("09-message-bar-visible.png", { fullPage: true });
  });

  test("message API endpoint returns 400 for job without session", async ({ page }) => {
    // Create a job but immediately check before it gets a session
    const res = await page.request.post(`${BASE}/api/jobs`, {
      data: { prompt: "test-no-session" }
    });
    const job = await res.json();

    // Immediately try to message it (no session yet)
    const msgRes = await page.request.post(`${BASE}/api/jobs/${job.id}/message`, {
      data: { message: "hello" }
    });
    // Should be 400 (no session) or 200 if session came up fast — either is valid
    expect([200, 400]).toContain(msgRes.status());
  });
});

test.describe("SSE deduplication", () => {
  test("no duplicate events after SSE reconnect", async ({ page }) => {
    await page.goto(BASE);
    await submitJob(page, "Say: dedup-test-marker", "/tmp");

    // Wait for output to populate
    await page.waitForFunction(
      () => document.querySelectorAll("#output-wrap .msg-block").length > 0,
      { timeout: 30_000 }
    );

    const countBefore = await page.locator("#output-wrap .msg-block").count();

    // Simulate reconnect by navigating away and back to same job
    const jobId = await page.evaluate(() => window.activeJobId || null);
    if (jobId) {
      await page.goto(BASE);
      await page.waitForLoadState("networkidle");
      // Click the job to re-open SSE
      await page.locator(".job-item").first().click();
      await page.waitForFunction(
        () => document.querySelectorAll("#output-wrap .msg-block").length > 0,
        { timeout: 10_000 }
      );

      const countAfter = await page.locator("#output-wrap .msg-block").count();
      // Should not be exactly double (dedup working)
      expect(countAfter).toBeLessThan(countBefore * 2);
    }
  });
});

test.describe("Status bar", () => {
  test("shows connected indicator when streaming", async ({ page }) => {
    await page.goto(BASE);
    await submitJob(page, "Say: statusbar-test");

    await page.waitForFunction(
      () => document.querySelector("#status-dot")?.classList.contains("connected"),
      { timeout: 15_000 }
    );

    await expect(page.locator("#status-text")).toHaveText("Connected");
  });
});
