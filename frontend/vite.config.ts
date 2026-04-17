import path from "node:path";
import react from "@vitejs/plugin-react-swc";
import { defineConfig } from "vite";

// Vite dev server config. Host pinned to 127.0.0.1 to match the backend's
// CORS allow-list; strictPort so we fail loudly if 1313 is taken rather than
// drifting onto a random port Triage can't reach.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    host: "127.0.0.1",
    port: 1313,
    strictPort: true,
  },
});
