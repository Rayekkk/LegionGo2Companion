// SPDX-License-Identifier: BSD-3-Clause
// Check the generated artifact: unresolved UI/React imports break Steam CEF.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

const code = readFileSync(new URL("../dist/index.js", import.meta.url), "utf8");
const source = ts.createSourceFile("index.js", code, ts.ScriptTarget.ES2020, true, ts.ScriptKind.JS);
assert.equal(source.parseDiagnostics.length, 0, "Generated bundle must parse as JavaScript");
const identifiers = new Set();
const imports = [];
function visit(node) {
  if (ts.isIdentifier(node)) identifiers.add(node.text);
  if (ts.isImportDeclaration(node)) imports.push(node.moduleSpecifier.text);
  ts.forEachChild(node, visit);
}
visit(source);
for (const name of ["react", "react/jsx-runtime", "react-dom", "@decky/ui", "@decky/manifest"])
  assert.ok(!imports.includes(name), `Unresolved Decky external: ${name}`);
for (const name of ["SP_REACT", "SP_JSX", "DFL"])
  assert.ok(identifiers.has(name), `Missing Steam global: ${name}`);
assert.ok(source.statements.some(node => ts.isExportAssignment(node) || ts.isExportDeclaration(node)),
  "Decky bundle must export the plugin");
const map = JSON.parse(readFileSync(new URL("../dist/index.js.map", import.meta.url), "utf8"));
assert.ok(map.sources.length && map.sourcesContent.every(value => typeof value === "string"),
  "Source map must retain sources for inspection and relinking");
console.log("Built frontend verified: JavaScript, Steam globals, plugin export and source map.");
