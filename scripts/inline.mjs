// Inlines src/core.js into the page template at the /*@@CORE@@*/ marker.
// The page runs as one classic script, so the module's `export` keywords are removed on the way in.
export const MARKER = "/*@@CORE@@*/";

export function inline(page, core, stamp = ""){
  if (page.split(MARKER).length !== 2) throw new Error(`Expected exactly one ${MARKER} in the page template`);
  const body = core.replace(/^export\s+/gm, "");
  if (/^\s*(import|export)\b/m.test(body)) throw new Error("core.js has an import or export the build can't inline");
  return page.replace(MARKER, () => `// ---- core.js${stamp ? ` (built ${stamp})` : ""} ----\n${body}// ---- end core.js ----`);
}
