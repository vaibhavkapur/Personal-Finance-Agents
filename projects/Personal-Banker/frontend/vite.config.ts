import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In development the API runs on :8000; in production the API serves dist/.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": "http://localhost:8000",
      "/healthz": "http://localhost:8000",
    },
  },
  build: { outDir: "dist", emptyOutDir: true },
});
