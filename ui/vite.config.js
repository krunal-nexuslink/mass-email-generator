import { defineConfig } from "vite";

export default defineConfig({
  server: {
    proxy: {
      "/mass_generate_email": "http://localhost:7000",
    },
  },
});
