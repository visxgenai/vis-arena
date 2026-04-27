import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  server: {
    allowedHosts: ["localhost", "arch", "vis-arena.jacobsun.xyz"],
    port: 8200,
  },
  plugins: [react()],
});
