import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/v1": "http://127.0.0.1:8000",
      "/healthz": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "../src/web/assets",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        manualChunks(id) {
          if (/node_modules\/(?:@xyflow|d3-|zustand)/.test(id)) return "graph";
          if (
            /node_modules\/(?:react-markdown|remark-|rehype-|unified|micromark|mdast-|hast-)/.test(
              id,
            )
          )
            return "markdown";
        },
      },
    },
  },
});
