// @ts-check
const { defineConfig, devices } = require("@playwright/test");

module.exports = defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  retries: 0,
  workers: 1,           // serial — tests share the running server

  use: {
    baseURL: "http://localhost:8888",
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
    video: "off",
  },

  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],

  // Snapshots go here
  snapshotDir: "./tests/e2e/snapshots",
  snapshotPathTemplate: "{snapshotDir}/{testFilePath}/{arg}{ext}",
});
