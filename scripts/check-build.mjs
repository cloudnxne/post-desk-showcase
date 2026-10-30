// Smoke check on the built page: the inline script must parse, and every core function must be inside it.
import { readFile } from "node:fs/promises";
import { MARKER } from "./inline.mjs";

const html = await readFile(new URL("../dist/index.html", import.meta.url), "utf8");
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) throw new Error("No inline <script> found in dist/index.html");

for (const s of scripts) new Function(s); // throws a SyntaxError if the script doesn't parse

const needed = ["parseSitemap", "rankUrls", "buildSeoHTML", "insertLinks", "postPlain", "locateAnchor", "checkLinks", "stripMarker", "metaStatus"];
const missing = needed.filter(n => !new RegExp(`(function ${n}\\b|const ${n} =)`).test(scripts.join("\n")));
if (missing.length) throw new Error("Missing from built page: " + missing.join(", "));
if (html.includes(MARKER)) throw new Error("Core marker was not replaced");

console.log(`dist/index.html OK: ${scripts.length} script(s) parse, all ${needed.length} core functions present`);
