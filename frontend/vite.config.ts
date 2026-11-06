/// <reference types="vitest/config" />
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  // Tailwind writes one static stylesheet at build time, so the strict CSP needs no inline styles.
  plugins: [react(), tailwindcss()],
  // Only for `npm run dev`: the API is behind the gateway, which scripts/dev.sh forwards to port
  // 9443. Node checks the gateway's certificate against the cluster's CA, in NODE_EXTRA_CA_CERTS.
  server: {
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: "https://localhost:9443", changeOrigin: true, secure: true },
    },
  },
  build: {
    // No source maps in the image, so the browser gets only what it runs.
    sourcemap: false,
  },
  test: {
    environment: "jsdom",
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
