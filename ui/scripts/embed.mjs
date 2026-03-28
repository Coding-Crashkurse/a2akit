/**
 * Copies dist/index.html into src/a2akit/_static/chat.html so the
 * debug UI is bundled into the Python package with zero runtime
 * dependencies.
 *
 * Usage: npm run build && npm run embed
 */
import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const distHtml = readFileSync(resolve(__dirname, "../dist/index.html"), "utf-8");

const outDir = resolve(__dirname, "../../src/a2akit/_static");
mkdirSync(outDir, { recursive: true });

const outPath = resolve(outDir, "chat.html");
writeFileSync(outPath, distHtml, "utf-8");

const sizeKb = (Buffer.byteLength(distHtml) / 1024).toFixed(1);
console.log(`Embedded ${sizeKb} KB HTML into ${outPath}`);
