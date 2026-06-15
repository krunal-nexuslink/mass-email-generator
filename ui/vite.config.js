import { defineConfig, loadEnv } from "vite";
import path from "path";
import { fileURLToPath } from "url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig(({ mode }) => {
  // Load .env from project root (parent of ui/)
  const env = loadEnv(mode, path.resolve(__dirname, ".."), "");
  return {
    server: {
      proxy: {
        "/mass_generate_email": env.VITE_API_URL || "http://localhost:7000",
        "/auth": env.VITE_API_URL || "http://localhost:7000",
      },
    },
  };
});
