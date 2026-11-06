import { defineConfig, devices } from "@playwright/test";

const spki = process.env.GATEWAY_SPKI ?? "";

export default defineConfig({
  testDir: "e2e",
  // Waiting for a fresh authenticator code can take 30 seconds.
  timeout: 120_000,
  retries: 0,
  // One at a time: the gateway allows 5 sign-ins a minute from one address (SRS 7.5), and this
  // stays within it.
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL ?? "https://localhost:9443",
    // Trust exactly the gateway's key, which make frontend-check reads from the cluster. Any other
    // certificate still fails, so this is not turning certificate checks off.
    launchOptions: { args: spki ? [`--ignore-certificate-errors-spki-list=${spki}`] : [] },
    trace: "retain-on-failure",
  },
  // UX-001: every screen works on a phone as well as a desktop.
  projects: [
    { name: "desktop", use: { viewport: { width: 1280, height: 800 } } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
});
