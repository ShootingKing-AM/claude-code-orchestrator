// @ts-check
/**
 * E2E test: Job stuck in LIMITED state after server restart should resume automatically.
 *
 * The bug: _last_limit_text_from_logs() was matching "rate limit" text inside normal
 * conversation content (tool outputs), causing a false positive. This led to the job
 * using LIMIT_RETRY_SECONDS fallback instead of the real resetsAt timestamp, and the
 * error "interrupted by server restart" was never cleared.
 *
 * What we verify:
 * 1. After restart, job enters _wait_and_resume (processes_spawned=0, countdown running)
 * 2. Error "interrupted by server restart" is cleared
 * 3. Force Resume button is visible and works (triggers actual run, processes_spawned > 0)
 */

const { test, expect } = require("@playwright/test");
const { execSync, spawn } = require("child_process");
const path = require("path");
const fs = require("fs");

const BASE = "http://localhost:8888";
const STUCK_JOB_ID = "494498ea-805c-4781-91b7-7de86b5a011a";
const SCREENSHOT_DIR = path.join(__dirname, "snapshots", "limited-resume");

function ensureDir(dir) {
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true });
}

async function screenshot(page, name) {
  ensureDir(SCREENSHOT_DIR);
  const p = path.join(SCREENSHOT_DIR, `${name}.png`);
  await page.screenshot({ path: p, fullPage: true });
  console.log(`  📸 ${p}`);
  return p;
}

test.describe("LIMITED job auto-resume after restart", () => {
  test("stuck LIMITED job enters countdown + force-resume works", async ({ page }) => {
    test.setTimeout(180_000);

    // ── Step 1: Navigate to the stuck job ─────────────────────────────────
    await page.goto(BASE);
    await page.waitForLoadState("networkidle");

    const jobItems = page.locator(".job-item");
    const count = await jobItems.count();
    console.log(`  Found ${count} jobs in sidebar`);

    let foundJob = false;
    for (let i = 0; i < count; i++) {
      const item = jobItems.nth(i);
      const text = await item.innerText();
      if (text.includes("LIMITED") && text.includes("Screenshots")) {
        await item.click();
        foundJob = true;
        console.log(`  Clicked Screenshots job (LIMITED)`);
        break;
      }
    }

    if (!foundJob) {
      for (let i = 0; i < count; i++) {
        const item = jobItems.nth(i);
        const text = await item.innerText();
        if (text.includes("LIMITED")) {
          await item.click();
          foundJob = true;
          console.log(`  Clicked first LIMITED job`);
          break;
        }
      }
    }

    await page.waitForTimeout(1000);
    await screenshot(page, "01-before-restart-limited-state");

    // ── Step 2: Check current state via API ───────────────────────────────
    const jobRes = await page.request.get(`${BASE}/api/jobs/${STUCK_JOB_ID}`);
    const jobData = await jobRes.json();
    console.log(`  Job state: ${jobData.state}, error: ${jobData.error}, resumes: ${jobData.resume_count}`);

    expect(jobData.state).toBe("paused_due_to_limit");

    // ── Step 3: Check what's in the last log entries ───────────────────────
    const logsRes = await page.request.get(`${BASE}/api/jobs/${STUCK_JOB_ID}/logs`);
    const logsData = await logsRes.json();
    const lastLogs = logsData.slice(-5);
    console.log(`  Last 5 log event types: ${lastLogs.map(l => l.event_type).join(", ")}`);

    // ── Step 4: Shutdown server gracefully and restart ─────────────────────
    console.log("  Shutting down server...");
    try {
      await page.request.post(`${BASE}/api/shutdown`);
    } catch (e) {
      // Expected — server closes connection
    }
    await page.waitForTimeout(2000);

    // Restart server
    console.log("  Restarting server...");
    const serverProcess = spawn("python3", ["-m", "web.server"], {
      cwd: "/home/sk/Documents/orch",
      detached: true,
      stdio: "ignore",
    });
    serverProcess.unref();

    // Wait for server to come back up
    let serverUp = false;
    for (let i = 0; i < 20; i++) {
      await page.waitForTimeout(500);
      try {
        const r = await page.request.get(`${BASE}/api/stats`);
        if (r.ok()) { serverUp = true; break; }
      } catch {}
    }
    console.log(`  Server back up: ${serverUp}`);
    expect(serverUp).toBe(true);

    // Give scheduler time to drain and enter _wait_and_resume
    await page.waitForTimeout(3000);

    // ── Step 5: Navigate back and check state ─────────────────────────────
    await page.goto(BASE);
    await page.waitForLoadState("networkidle");

    const items2 = page.locator(".job-item");
    const count2 = await items2.count();
    for (let i = 0; i < count2; i++) {
      const item = items2.nth(i);
      const text = await item.innerText();
      if (text.includes("Screenshots")) {
        await item.click();
        break;
      }
    }
    await page.waitForTimeout(2000);
    await screenshot(page, "02-after-restart-state");

    // Check job state
    const jobRes2 = await page.request.get(`${BASE}/api/jobs/${STUCK_JOB_ID}`);
    const jobData2 = await jobRes2.json();
    console.log(`  Job state after restart: ${jobData2.state}, error: ${jobData2.error}`);

    // The error should be cleared now (fix: job.error = None before transition)
    expect(jobData2.error).toBeNull();

    // ── Step 6: Verify Force Resume button is visible ─────────────────────
    const forceBtn = page.locator("#force-resume-btn");
    const forceBtnVisible = await forceBtn.isVisible();
    console.log(`  Force Resume button visible: ${forceBtnVisible}`);
    await screenshot(page, "03-force-resume-button");

    // Force Resume button should be visible because _wait_and_resume is running
    expect(forceBtnVisible).toBe(true);

    // ── Step 7: Click Force Resume and verify job actually starts ─────────
    console.log("  Clicking Force Resume...");
    await forceBtn.click();
    await page.waitForTimeout(5000); // give time for job to start

    await screenshot(page, "04-after-force-resume");

    const statsRes = await page.request.get(`${BASE}/api/stats`);
    const stats = await statsRes.json();
    console.log(`  processes_spawned: ${stats.processes_spawned}, jobs_running: ${stats.jobs_running}`);

    // After force resume, the scheduler should have spawned a claude process
    expect(stats.processes_spawned).toBeGreaterThan(0);

    await screenshot(page, "05-final-state");
  });
});
