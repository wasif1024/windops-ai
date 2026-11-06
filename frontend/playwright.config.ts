import { defineConfig } from "@playwright/test";

const spki = process.env.GATEWAY_SPKI ?? "";

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL ?? "https://localhost:9443",
    // Trust exactly the gateway's key, which make frontend-check reads from the cluster. Any other
    // certificate still fails, so this is not turning certificate checks off.
    launchOptions: { args: spki ? [`--ignore-certificate-errors-spki-list=${spki}`] : [] },
    trace: "retain-on-failure",
  },
});
