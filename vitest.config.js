import { defineConfig } from "vitest/config";

export default defineConfig({
  test: {
    environment: "jsdom",
    include: ["test/**/*.test.js"],
    coverage: { include: ["src/core.js"], reporter: ["text", "html"] },
  },
});
