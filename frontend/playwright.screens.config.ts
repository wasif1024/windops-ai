// The README's console screenshots, captured in a real browser by `make readme-screens`. Kept apart
// from the browser checks in e2e/, which `make frontend-check` runs.
import { defineConfig, devices } from "@playwright/test";

const spki = process.env.GATEWAY_SPKI ?? "";

export default defineConfig({
  testDir: "screens",
  timeout: 240_000,
  retries: 0,
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: process.env.BASE_URL ?? "https://localhost:9443",
    launchOptions: { args: spki ? [`--ignore-certificate-errors-spki-list=${spki}`] : [] },
    colorScheme: "light",
  },
  projects: [
    { name: "desktop", use: { viewport: { width: 1440, height: 900 } } },
    { name: "phone", use: { ...devices["Pixel 7"] } },
  ],
});
