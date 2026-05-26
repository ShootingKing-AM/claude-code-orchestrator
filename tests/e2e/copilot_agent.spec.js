// @ts-check
/**
 * E2E Playwright tests for Copilot Agent backend (tool-augmented coding agent).
 * TDD: these tests define the expected behaviour before implementation.
 *
 * Run:  npx playwright test tests/e2e/copilot_agent.spec.js --reporter=list
 */
const { test, expect } = require("@playwright/test");
const fs = require("fs");
const path = require("path");

const BASE = "http://localhost:8888";

// ── helpers ──────────────────────────────────────────────────────────────────

async function createJob(page, { prompt, backend = "copilot-agent", model = "" } = {}) {
  await page.click("#new-job-btn");
  await page.waitForSelector("#modal-overlay.open");
  await page.fill("#prompt-input", prompt);
  await page.selectOption("#backend-select", backend);
  if (model) await page.selectOption("#model-input", model);
  await page.click("#modal-submit");
  // Wait for the detail panel to open and start streaming
  await page.waitForSelector("#output-wrap", { state: "visible", timeout: 10_000 });
}

async function waitForJobDone(page, timeout = 120_000) {
  await page.waitForFunction(
    () => {
      const el = document.querySelector("#meta-state .badge");
      return el && /done|failed/.test(el.textContent.trim().toLowerCase());
    },
    { timeout }
  );
}

// ── tests ─────────────────────────────────────────────────────────────────────

test.describe("Copilot Agent backend", () => {
  test.setTimeout(150_000);

  // ── 1. /api/backends includes copilot-agent ───────────────────────────────
  test("backends endpoint reports copilot-agent available", async ({ request }) => {
    const res = await request.get(`${BASE}/api/backends`);
    expect(res.ok()).toBeTruthy();
    const body = await res.json();
    expect(body.backends).toContain("copilot-agent");
  });

  // ── 2. UI dropdown has copilot-agent option ───────────────────────────────
  test("job creation modal has copilot-agent backend option", async ({ page }) => {
    await page.goto(BASE);
    await page.click("#new-job-btn");
    await page.waitForSelector("#modal-overlay.open");

    const options = await page.$$eval(
      "#backend-select option",
      (opts) => opts.map((o) => o.value)
    );
    expect(options).toContain("copilot-agent");
  });

  // ── 3. copilot-agent job reads a file via tool use ───────────────────────
  test("copilot-agent job uses read_file tool and reports contents", async ({ page }) => {
    // Create a sentinel file the agent must read
    const sentinelPath = "/tmp/orch-agent-test.txt";
    fs.writeFileSync(sentinelPath, "ORCH_AGENT_SENTINEL_42\n");

    await page.goto(BASE);
    await createJob(page, {
      prompt: `Read the file ${sentinelPath} and tell me exactly what text it contains. Use the read_file tool.`,
      backend: "copilot-agent",
    });

    // Wait for a job to appear and be selected in the detail panel
    await page.waitForSelector("#output-wrap", { state: "visible", timeout: 10_000 });

    // Wait for completion
    await waitForJobDone(page, 120_000);

    // Verify tool_use event was rendered (teal left-border block)
    const toolBlock = page.locator(".tool-block").first();
    await expect(toolBlock).toBeVisible({ timeout: 5_000 });
    await expect(toolBlock).toContainText("read_file");

    // Verify the output contains the sentinel text
    const outputWrap = page.locator("#output-wrap");
    await expect(outputWrap).toContainText("ORCH_AGENT_SENTINEL_42", { timeout: 5_000 });

    // Badge should say [Copilot Agent]
    const badge = page.locator(".backend-badge");
    await expect(badge).toContainText(/Copilot Agent/i);
  });

  // ── 4. copilot-agent writes a file ──────────────────────────────────────
  test("copilot-agent job writes a file via write_file tool", async ({ page }) => {
    const outputPath = "/tmp/orch-agent-written.txt";
    // Remove if exists
    try { fs.unlinkSync(outputPath); } catch (_) {}

    await page.goto(BASE);
    await createJob(page, {
      prompt: `Write the text "AGENT_WROTE_THIS" to the file ${outputPath} using the write_file tool, then confirm you did it.`,
      backend: "copilot-agent",
    });

    await page.waitForSelector("#output-wrap", { state: "visible", timeout: 10_000 });
    await waitForJobDone(page, 120_000);

    // Verify tool_use event for write_file
    const toolBlock = page.locator(".tool-block").first();
    await expect(toolBlock).toBeVisible({ timeout: 5_000 });
    await expect(toolBlock).toContainText("write_file");

    // Verify file was actually written
    expect(fs.existsSync(outputPath)).toBe(true);
    const written = fs.readFileSync(outputPath, "utf8");
    expect(written).toContain("AGENT_WROTE_THIS");
  });

  // ── 5. copilot-agent job runs bash ────────────────────────────────────────
  test("copilot-agent job runs bash command via run_bash tool", async ({ page }) => {
    await page.goto(BASE);
    await createJob(page, {
      prompt: `Run the bash command "echo BASH_OK_ORCH" using the run_bash tool and report the output.`,
      backend: "copilot-agent",
    });

    await page.waitForSelector("#output-wrap", { state: "visible", timeout: 10_000 });
    await waitForJobDone(page, 120_000);

    // Verify tool_use event
    const toolBlock = page.locator(".tool-block").first();
    await expect(toolBlock).toBeVisible({ timeout: 5_000 });
    await expect(toolBlock).toContainText("run_bash");

    // Verify output mentions the echo result
    const outputWrap = page.locator("#output-wrap");
    await expect(outputWrap).toContainText("BASH_OK_ORCH", { timeout: 5_000 });
  });

  // ── 6. detail panel shows Copilot Agent badge ────────────────────────────
  test("job detail panel shows Copilot Agent backend badge", async ({ page }) => {
    await page.goto(BASE);
    await createJob(page, {
      prompt: "Say hello in one sentence.",
      backend: "copilot-agent",
    });

    await page.waitForSelector("#output-wrap", { state: "visible", timeout: 10_000 });
    await waitForJobDone(page, 120_000);

    const badge = page.locator(".backend-badge");
    await expect(badge).toBeVisible();
    await expect(badge).toContainText(/Copilot Agent/i);
  });

  // ── 7. screenshot: completed copilot-agent job ────────────────────────────
  test("screenshot: copilot-agent job with tool use completed state", async ({ page }) => {
    const sentinelPath = "/tmp/orch-agent-screenshot.txt";
    fs.writeFileSync(sentinelPath, "SCREENSHOT_SENTINEL\n");

    await page.goto(BASE);
    await createJob(page, {
      prompt: `Read the file ${sentinelPath} using read_file and summarize its content in one sentence.`,
      backend: "copilot-agent",
    });

    await page.waitForSelector("#output-wrap", { state: "visible", timeout: 10_000 });
    await waitForJobDone(page, 120_000);

    await page.screenshot({
      path: "test-results/copilot-agent-completed.png",
      fullPage: false,
    });

    const badge = page.locator(".backend-badge");
    await expect(badge).toContainText(/Copilot Agent/i);
  });
});
