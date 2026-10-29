// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/build/rollup.config.js): change it there, then `kit sync`.
//
// Adapted from @decky/rollup 1.0.2, Copyright (c) 2024 Steam Deck Homebrew, BSD-3-Clause (its globals map).
// Sources: DeckyLS/FSR4Injector (own config without @decky/rollup, which pulled in vulnerable glob dependencies;
// decky:// source map paths on Windows too), Ayaneo3Companion/LegionGo2Companion (react-dom global, NODE_ENV and
// __PLUGIN_VERSION__ replaced, commonjs/json, smallest tree-shaking with @decky side-effect free, guarded dist
// clean-up that can keep a native build such as dist/desktop).
// Guarantees: React, ReactDOM and @decky/ui come from Steam's globals (SP_REACT, SP_JSX, SP_REACTDOM, DFL), never
// bundled; @decky/manifest is plugin.json; an import that resolves to nothing fails the build instead of becoming a
// silent external; process.env.NODE_ENV and __PLUGIN_VERSION__ are replaced (whole words, never an assignment
// target) with an exact source map; dist/ is emptied before each build only when it is a real folder right under
// the plugin (never through a symlink), keeping the entries listed in package.json "decky.keepDist"; every source
// map path is a decky:// URL relative to the plugin, also when tsconfig's rootDir is the plugin itself (DeckyEQ: src/
// imports ../package.json and a JSON file of its backend).
// Deliberately not here: image assets (no plugin ships them through rollup yet); @rollup/plugin-commonjs and
// @rollup/plugin-json are used only when the plugin installs them (a plugin without CommonJS/JSON imports does not
// carry their dependencies).
import { existsSync, lstatSync, readdirSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import typescript from "@rollup/plugin-typescript";
import { nodeResolve } from "@rollup/plugin-node-resolve";
import externalGlobals from "rollup-plugin-external-globals";
import MagicString from "magic-string";

const root = realpathSync(dirname(fileURLToPath(import.meta.url)));
const dist = join(root, "dist");
const manifest = JSON.parse(readFileSync(join(root, "plugin.json"), "utf8"));
const keepDist = JSON.parse(readFileSync(join(root, "package.json"), "utf8")).decky?.keepDist ?? [];
const optional = async name => (await import(name).catch(() => null))?.default;
const commonjs = await optional("@rollup/plugin-commonjs");
const json = await optional("@rollup/plugin-json");
const constants = { "process.env.NODE_ENV": JSON.stringify(process.env.NODE_ENV || "production"),
  __PLUGIN_VERSION__: JSON.stringify(manifest.version) };
const pattern = new RegExp(`(?<![\\w$.])(${Object.keys(constants).map(k => k.replace(/[.$]/g, "\\$&")).join("|")})`
  + "(?![\\w$])(?!\\s*=(?!=))", "g");
const decky = /[\\/]node_modules[\\/]@decky[\\/](api|ui)[\\/]/;

export default {
  input: join(root, "src", "index.tsx"), context: "window",
  external: ["react", "react-dom", "@decky/ui"],
  treeshake: { preset: "smallest", moduleSideEffects: id => id !== "@decky/ui" && !decky.test(id) },
  onwarn(warning, warn) {
    if (warning.code === "UNRESOLVED_IMPORT") throw new Error(`Unresolved import: ${warning.message}`);
    warn(warning);
  },
  plugins: [
    { name: "clean-plugin-dist", buildStart() {
      if (!existsSync(dist)) return;
      const info = lstatSync(dist);
      if (info.isSymbolicLink() || !info.isDirectory() || realpathSync(dist) !== dist || dirname(dist) !== root)
        throw new Error(`Refusing to clean an unexpected dist path: ${dist}`);
      for (const entry of readdirSync(dist))
        if (!keepDist.includes(entry)) rmSync(join(dist, entry), { recursive: true, force: true });
    } },
    { name: "replace-constants", transform(code, id) {
      if (!code.includes("process.env.NODE_ENV") && !code.includes("__PLUGIN_VERSION__")) return null;
      const out = new MagicString(code);
      for (const m of code.matchAll(pattern)) out.overwrite(m.index, m.index + m[0].length, constants[m[0]]);
      return out.hasChanged() ? { code: out.toString(), map: out.generateMap({ hires: true, source: id }) } : null;
    } },
    typescript(), ...(commonjs ? [commonjs()] : []), ...(json ? [json()] : []), nodeResolve({ browser: true }),
    externalGlobals({ react: "SP_REACT", "react/jsx-runtime": "SP_JSX", "react-dom": "SP_REACTDOM",
      "@decky/ui": "DFL", "@decky/manifest": JSON.stringify(manifest) })],
  output: { dir: dist, format: "esm", exports: "default", sourcemap: true,
    // Rollup hands this hook native separators; on Windows the prefix only matches once they are forward slashes.
    // shortcut: every leading ../ is dropped (TypeScript's rootDir "." adds one more than dist/ needs), so a source
    // outside the plugin would be labelled as its own; no plugin imports from outside its folder.
    sourcemapPathTransform: path => path.replace(/\\/g, "/").replace(/^(\.\.\/)+/,
      `decky://decky/plugin/${encodeURIComponent(manifest.name)}/`) },
};
