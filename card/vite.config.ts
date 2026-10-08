/// <reference types="vitest/config" />
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

// Die Integration liefert die Karte selbst aus (siehe card.py) — der Build landet
// deshalb direkt im Integrationsordner und kommt mit dem Release-ZIP zu HACS.
const outDir = fileURLToPath(
  new URL("../custom_components/delivery_tracker/frontend", import.meta.url),
);

export default defineConfig({
  build: {
    outDir,
    // Liegt ausserhalb von card/ — Vite leert den Ordner nur auf ausdruecklichen Wunsch.
    emptyOutDir: true,
    target: "es2022",
    sourcemap: false,
    lib: {
      entry: "src/delivery-tracker-card.ts",
      formats: ["es"],
      fileName: () => "delivery-tracker-card.js",
    },
  },
  // Lit prueft im Dev-Modus zusaetzlich und warnt in der Konsole; die Karte
  // wird immer als Produktions-Build ausgeliefert.
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  test: {
    environment: "happy-dom",
    include: ["test/**/*.test.ts"],
  },
});
