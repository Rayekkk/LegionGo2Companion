// SPDX-License-Identifier: BSD-3-Clause
// Adapted from @decky/rollup 1.0.2, Copyright (c) 2024 Steam Deck Homebrew.
// See LICENSE.DECKY-ROLLUP. Keep Decky's globals without the old glob-based
// deletion plugin and its unpatched braces dependency.
import { existsSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import commonjs from "@rollup/plugin-commonjs";
import json from "@rollup/plugin-json";
import { nodeResolve } from "@rollup/plugin-node-resolve";
import replace from "@rollup/plugin-replace";
import typescript from "@rollup/plugin-typescript";
import externalGlobals from "rollup-plugin-external-globals";
import importAssets from "rollup-plugin-import-assets";

const root = dirname(fileURLToPath(import.meta.url));
const output = resolve(root, "dist");
if (dirname(output) !== root) throw new Error("Build output must stay within the project");
const manifest = JSON.parse(readFileSync(resolve(root, "plugin.json"), "utf8"));
const { version } = JSON.parse(readFileSync(resolve(root, "package.json"), "utf8"));

export default {
  input: resolve(root, "src/index.tsx"),
  context: "window",
  external: ["react", "react-dom", "@decky/ui"],
  plugins: [
    // dist/desktop holds the separately built native app; clean only rollup's own output.
    { name: "clean-output", buildStart() {
      for (const name of existsSync(output) ? readdirSync(output) : [])
        if (name !== "desktop") rmSync(resolve(output, name), { recursive: true, force: true });
    } },
    typescript(), json(), commonjs(), nodeResolve({ browser: true }),
    externalGlobals({
      react: "SP_REACT", "react/jsx-runtime": "SP_JSX", "react-dom": "SP_REACTDOM",
      "@decky/ui": "DFL", "@decky/manifest": JSON.stringify(manifest),
    }),
    replace({ preventAssignment: false,
      "process.env.NODE_ENV": JSON.stringify(process.env.NODE_ENV || "production"),
      __PLUGIN_VERSION__: JSON.stringify(version) }),
    importAssets({ publicPath: `http://127.0.0.1:1337/plugins/${manifest.name}/` }),
  ],
  treeshake: { preset: "smallest", pureExternalImports: { pure: ["@decky/ui", "@decky/api"] } },
  output: { dir: output, format: "esm", sourcemap: true, exports: "default",
    sourcemapPathTransform: source => source.replace(/^\.\.\//,
      `decky://decky/plugin/${encodeURIComponent(manifest.name)}/`) },
};
