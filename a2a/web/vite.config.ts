/// <reference types="vitest" />
import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  // Pinned, not incidental: the bus's websocket listener renders
  // `allowed_origins: ["http://localhost:5173", "http://127.0.0.1:5173"]`,
  // so a page served from vite's fallback port would be refused at the
  // handshake with a 403. `strictPort` fails loudly instead of drifting to
  // 5174 and leaving a mysterious connect failure.
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
