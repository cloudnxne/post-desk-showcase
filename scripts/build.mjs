// Builds dist/index.html: the page template with src/core.js inlined, so the tested code is the shipped code.
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { inline } from "./inline.mjs";

const page = await readFile(new URL("../src/page.html", import.meta.url), "utf8");
const core = await readFile(new URL("../src/core.js", import.meta.url), "utf8");
const out = inline(page, core, new Date().toISOString());

await mkdir(new URL("../dist/", import.meta.url), { recursive: true });
await writeFile(new URL("../dist/index.html", import.meta.url), out);
console.log(`Built dist/index.html (${(out.length / 1024).toFixed(1)} KB)`);
