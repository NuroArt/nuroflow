/// <reference types="vitest" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: the API runs on :8000 and Vite proxies /api to it. Same origin for the
// browser, so no CORS preflights and no credentials in cross-site requests.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: false } },
  },
  build: { sourcemap: true, chunkSizeWarningLimit: 700 },
  test: { environment: "jsdom", setupFiles: ["./src/test/setup.ts"], css: false },
});
