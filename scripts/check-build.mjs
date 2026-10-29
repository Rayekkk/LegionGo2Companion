// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/build/check-build.mjs): change it there, then `kit sync`.
//
// Checks the generated dist/index.js the way Steam's CEF will load it. Sources: DeckyLS/FSR4Injector/DeckyReshade
// (parse, externals, globals, export, source map), LegionGo2Companion/Ayaneo3Companion (no unreplaced
// __PLUGIN_VERSION__), DeckyVibranceHDR verify_package.py and tests/panel-harness.mjs (decky:// sources, source
// map not older than src/, every src file and @decky/api in the map, the bundle loaded against a fake Decky host).
// Guarantees: the bundle parses as JavaScript and has no import left at all (React, ReactDOM, @decky/ui and
// @decky/manifest come from Steam's globals; anything else would fail in Steam); it uses SP_REACT, SP_JSX and DFL;
// it exports the plugin; no build-time placeholder is left; the source map carries every source as a decky:// URL,
// includes @decky/api and every src/ file that has runtime code, and its copy of each src/ file equals the file on
// disk (a stale dist/ fails); the bundle loads against a stand-in Decky host (Steam's globals, SteamClient and
// appStore too, answer anything, so a plugin that registers for Steam events when it starts loads), connects to the
// loader under plugin.json's name and its default export gives a plugin with that name, a content element and an
// icon (DeckyVibranceHDR build-contract); every source in the map is code that ships (src/, node_modules/ or a
// package.json "decky.files" entry), so a browser preview or a test helper pulled into the bundle fails (DeckyEQ
// package.py kept its preview out); a promise the plugin rejects while loading fails the check; the check ends once
// it has passed, even when the plugin starts timers at load.
// Deliberately not here: rendering the panel (each plugin's tests/*.test.cjs does that on the source).
import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = resolve(fileURLToPath(new URL("..", import.meta.url)));
const ts = createRequire(join(root, "package.json"))("typescript");
const manifest = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8"));
const code = readFileSync(join(root, "dist", "index.js"), "utf8");
const source = ts.createSourceFile("index.js", code, ts.ScriptTarget.ES2020, true, ts.ScriptKind.JS);
assert.equal(source.parseDiagnostics.length, 0, "Generated bundle must parse as JavaScript");
const identifiers = new Set();
const imports = [];
function visit(node) {
  if (ts.isIdentifier(node)) identifiers.add(node.text);
  if (ts.isImportDeclaration(node)) imports.push(node.moduleSpecifier.text);
  if (ts.isCallExpression(node) && node.expression.kind === ts.SyntaxKind.ImportKeyword) imports.push("import()");
  ts.forEachChild(node, visit);
}
visit(source);
for (const name of imports) assert.fail(`Unresolved Decky external: ${name} (the bundle must import nothing)`);
for (const name of ["SP_REACT", "SP_JSX", "DFL"])
  assert.ok(identifiers.has(name), `Missing Steam global: ${name}`);
assert.ok(source.statements.some(node => ts.isExportAssignment(node) || ts.isExportDeclaration(node)),
  "Decky bundle must export the plugin");
assert.ok(!code.includes("__PLUGIN_VERSION__"), "Build placeholder __PLUGIN_VERSION__ was not replaced");

const map = JSON.parse(readFileSync(join(root, "dist", "index.js.map"), "utf8"));
assert.ok(map.sources.length && map.sourcesContent?.length === map.sources.length
  && map.sourcesContent.every(value => typeof value === "string"),
  "Source map must retain sources for inspection and relinking");
const prefix = `decky://decky/plugin/${encodeURIComponent(manifest.name)}/`;
const shipped = (JSON.parse(readFileSync(join(root, "package.json"), "utf8")).decky?.files ?? []).map(entry =>
  new RegExp(`^${entry.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replaceAll("*", "[^/]*")}(/|$)`));
const mapped = new Set();
map.sources.forEach((name, i) => {
  assert.ok(name.startsWith(prefix), `Source map path is not a decky:// URL: ${name}`);
  const path = decodeURIComponent(name.slice(prefix.length));
  assert.ok(/^(src|node_modules)\//.test(path) || shipped.some(rule => rule.test(path)),
    `${path} is in the bundle but does not ship (not in src/, node_modules/ or decky.files)`);
  mapped.add(path);
  if (path.startsWith("src/") && existsSync(join(root, path)))
    assert.equal(map.sourcesContent[i].replace(/\r\n/g, "\n"), readFileSync(join(root, path), "utf8").replace(/\r\n/g, "\n"),
      `dist/ is older than ${path}: build again`);
});
assert.ok([...mapped].some(p => p.startsWith("node_modules/@decky/api/")), "@decky/api must be in the source map");
const runtime = file => ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.ES2020, true)
  .statements.some(s => !(ts.isInterfaceDeclaration(s) || ts.isTypeAliasDeclaration(s)
    || (ts.isImportDeclaration(s) && s.importClause?.isTypeOnly)
    || (ts.isExportDeclaration(s) && s.isTypeOnly)));
const walk = dir => readdirSync(dir, { withFileTypes: true }).flatMap(e => e.isDirectory() ? walk(join(dir, e.name))
  : /\.tsx?$/.test(e.name) && !e.name.endsWith(".d.ts") ? [join(dir, e.name)] : []);
for (const file of walk(join(root, "src")))
  if (runtime(file)) {
    const path = relative(root, file).replace(/\\/g, "/");
    assert.ok(mapped.has(path), `${path} has code but is not in the bundle (stale dist/ or a dead file)`);
  }

// The bundle against a stand-in Decky host: Steam's globals answer anything, the loader records connections.
const anything = name => new Proxy(function () {}, { get: (t, k) => k === Symbol.toPrimitive ? () => String(name)
  : k === "then" ? undefined : anything(`${String(name)}.${String(k)}`), apply: () => undefined,
  construct: () => ({}) });
const connections = [];
const host = {
  SP_REACT: anything("SP_REACT"), SP_REACTDOM: anything("SP_REACTDOM"), DFL: anything("DFL"),
  SteamClient: anything("SteamClient"), appStore: anything("appStore"),
  SP_JSX: { jsx: (type, props) => ({ type, props }), jsxs: (type, props) => ({ type, props }), Fragment: "Fragment" },
  window: { __DECKY_SECRET_INTERNALS_DO_NOT_USE_OR_YOU_WILL_BE_FIRED_deckyLoaderAPIInit: {
    connect: (version, name) => { connections.push(name); return { _version: version,
      callable: () => async () => ({ ok: true }), toaster: { toast() {} }, addEventListener() {}, removeEventListener() {} }; },
  } },
};
const saved = Object.fromEntries(Object.keys(host).map(k => [k, globalThis[k]]));
const rejected = [];
process.on("unhandledRejection", reason => rejected.push(reason));
Object.assign(globalThis, host);
try {
  const bundle = await import(`data:text/javascript;base64,${Buffer.from(code).toString("base64")}`);
  const plugin = typeof bundle.default === "function" ? bundle.default() : bundle.default;
  assert.equal(plugin?.name, manifest.name, "The plugin's name must be plugin.json's name");
  assert.ok(plugin.content, "The plugin must have a content element");
  assert.ok(plugin.icon, "The plugin must have an icon (Decky's plugin list shows it)");
  assert.ok(connections.length === 0 || connections.every(n => n === manifest.name),
    `The bundle connected to the loader as ${connections.join(", ")}, not ${manifest.name}`);
  await new Promise(done => setTimeout(done, 0));  // what loading queued runs before the verdict
  assert.equal(rejected.length, 0, `The bundle rejected a promise while loading: ${rejected[0]}`);
} finally {
  for (const [k, v] of Object.entries(saved)) if (v === undefined) delete globalThis[k]; else globalThis[k] = v;
}
console.log("Built frontend verified: JavaScript, Steam globals, plugin export, source map and a load on a stand-in host.");
process.exit(0);  // the plugin's own timers (a poll started at load) would keep Node running
