import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const allowedHosts = (env.VITE_DEV_ALLOWED_HOSTS || "localhost,arch,vis-arena.jacobsun.xyz")
    .split(",")
    .map((host) => host.trim())
    .filter(Boolean);

  return {
    server: {
      allowedHosts,
      port: Number(env.VITE_DEV_SERVER_PORT || "8200"),
    },
    plugins: [react()],
  };
});
