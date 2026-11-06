/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // Tailwind writes one static stylesheet at build time, so the strict CSP needs no inline styles.
  plugins: [react(), tailwindcss()],
  build: {
    // No source maps in the image, so the browser gets only what it runs.
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
