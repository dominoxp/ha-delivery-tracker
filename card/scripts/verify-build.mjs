// Prueft das Build-Ergebnis: genau eine JavaScript-Datei, die nichts nachlaedt.
// Ein `import` oder `import()` im Bundle hiesse, dass der Browser zur Laufzeit
// eine weitere Datei holen muesste, die HA nicht ausliefert.
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const outDir = fileURLToPath(
  new URL("../../custom_components/delivery_tracker/frontend/", import.meta.url),
);
const expected = "delivery-tracker-card.js";

const files = readdirSync(outDir);
if (files.length !== 1 || files[0] !== expected) {
  console.error(`Erwartet genau ${expected} in ${outDir}, gefunden: ${files.join(", ")}`);
  process.exit(1);
}

const code = readFileSync(join(outDir, expected), "utf8");
const external = /(?:^|[;}\s])import\s*(?:[\w{*][^;]*?from\s*)?["']|\bimport\s*\(/m;
if (external.test(code)) {
  console.error(`${expected} enthaelt einen externen Import — alles muss gebuendelt sein.`);
  process.exit(1);
}

const kib = (Buffer.byteLength(code) / 1024).toFixed(1);
console.log(`Build ok: ${expected} (${kib} KiB), keine externen Importe.`);
