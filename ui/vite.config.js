import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// plugins: [react()] is load-bearing, not boilerplate. Without it esbuild's default JSX
// transform emits React.createElement calls, App.jsx never imports React, and the page
// renders blank with "React is not defined" -- while `vite build` still succeeds,
// because it is a runtime failure and not a compile one. A green build is not evidence
// the page works.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // The API is read-only and localhost-only; proxying keeps the browser same-origin
    // so no CORS preflight is needed in development.
    proxy: { "/api": { target: "http://127.0.0.1:8000", changeOrigin: true } },
  },
});
