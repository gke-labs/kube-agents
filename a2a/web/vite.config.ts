/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Pinned, not incidental: dev/nats.conf's websocket listener allows the
  // 5173 origin, so a page served from vite's fallback port would be refused
  // at the handshake with a 403. `strictPort` fails loudly instead of
  // drifting to 5174 and leaving a mysterious connect failure. (The install's
  // bus allows only the console server's origin, localhost:8080.)
  server: {
    port: 5173,
    strictPort: true,
  },
  build: {
    outDir: "dist",
  },
  test: {
    // Node by default; the component tests opt into jsdom with a
    // `@vitest-environment jsdom` docblock.
    environment: "node",
  },
});
