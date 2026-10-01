import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Use an explicit IPv4 loopback: Node resolves "localhost" to ::1 first, which can hit an unrelated service.
const target = process.env.VITE_PROXY_TARGET || "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/v1": { target, changeOrigin: true },
      "/healthz": { target, changeOrigin: true },
    },
  },
});
