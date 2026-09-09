#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(repoRoot, "plugin.json"), "utf8"));
const PLUGIN_DIR_NAME = "LegionGo2Companion";
const version = manifest.version;
const buildDir = join(repoRoot, "build");
const stageDir = join(buildDir, PLUGIN_DIR_NAME);
const zipPath = join(repoRoot, `${PLUGIN_DIR_NAME}-${version}.zip`);

const CONTENTS = [
  "main.py",
  "module_control.py",
  "module_runtime.py",
  "conflict_guard.py",
  "safe_settings.py",
  "system_process.py",
  "inputplumber_process.py",
  "companion_updates.py",
  "tdp_backend.py",
  "vibration_backend.py",
  "display_backend.py",
  "wifi_backend.py",
  "rgb_backend.py",
  "remap_backend.py",
  "battery_backend.py",
  "controller_backend.py",
  "controller_imu.py",
  "tdp_updater.py",
  "vibration_updater.py",
  "display_updater.py",
  "plugin.json",
  "package.json",
  "package-lock.json",
  "rollup.config.js",
  "tsconfig.json",
  "README.md",
  "CHANGELOG.md",
  "SOURCE.md",
  "LICENSE",
  "LICENSE.MIT",
  "LICENSE.HUESYNC",
  "NOTICE",
  "requirements.txt",
  "src",
  "gamescope",
  "pyudev",
  "dist",
];

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(1);
}

function validatePayload() {
  for (const entry of CONTENTS) {
    if (!existsSync(join(repoRoot, entry))) fail(`required file missing: ${entry}`);
  }
  const pkg = JSON.parse(readFileSync(join(repoRoot, "package.json"), "utf8"));
  const lock = JSON.parse(readFileSync(join(repoRoot, "package-lock.json"), "utf8"));
  const lockRoot = lock.packages?.[""];
  if (pkg.version !== version) {
    fail(`version mismatch: plugin.json=${version} package.json=${pkg.version}`);
  }
  if (!lockRoot || lock.name !== pkg.name || lock.version !== pkg.version ||
      lockRoot.name !== pkg.name || lockRoot.version !== pkg.version ||
      lockRoot.license !== pkg.license) {
    fail("package-lock.json root metadata does not match package.json");
  }
}

function stagePayload(destination) {
  mkdirSync(destination, { recursive: true });
  for (const entry of CONTENTS) {
    cpSync(join(repoRoot, entry), join(destination, entry), {
      recursive: true,
      filter: (src) => !/(__pycache__|\.pyc$|\.DS_Store|node_modules)/.test(src),
    });
  }

  const apiRoot = join(repoRoot, "node_modules", "@decky", "api");
  const apiPackage = JSON.parse(readFileSync(join(apiRoot, "package.json"), "utf8"));
  if (apiPackage.version !== "1.1.3") {
    fail(`NOTICE expects @decky/api 1.1.3, found ${apiPackage.version}`);
  }
  const licenseDir = join(destination, "THIRD_PARTY_LICENSES");
  mkdirSync(licenseDir, { recursive: true });
  cpSync(join(apiRoot, "LICENSE"), join(licenseDir, "decky-api-LGPL-2.1.txt"));
  cpSync(apiRoot, join(destination, "THIRD_PARTY_SOURCES", "decky-api-1.1.3"), {
    recursive: true,
  });
}

function sevenZip() {
  return [
    "C:\\Program Files\\7-Zip\\7z.exe",
    "C:\\Program Files (x86)\\7-Zip\\7z.exe",
  ].find(existsSync);
}

function checkStaging() {
  const root = mkdtempSync(join(tmpdir(), "legiongo2companion-package-check-"));
  try {
    stagePayload(join(root, PLUGIN_DIR_NAME));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
  console.log(`package payload check passed (${CONTENTS.length} entries)`);
}

function packagePlugin() {
  rmSync(buildDir, { recursive: true, force: true });
  rmSync(zipPath, { force: true });
  stagePayload(stageDir);

  const sevenZipPath = sevenZip();
  try {
    if (sevenZipPath) {
      execFileSync(sevenZipPath, ["a", "-tzip", "-mx=9", zipPath, PLUGIN_DIR_NAME], {
        cwd: buildDir,
        stdio: "inherit",
      });
    } else {
      execFileSync("zip", ["-r", "-9", "-q", zipPath, PLUGIN_DIR_NAME], {
        cwd: buildDir,
        stdio: "inherit",
      });
    }
  } catch (error) {
    fail(sevenZipPath
      ? `7-Zip failed: ${error.message}`
      : `no zip tool found: ${error.message}`);
  }
  rmSync(buildDir, { recursive: true, force: true });
  console.log(`packaged v${version} -> ${zipPath}`);
}

validatePayload();
if (process.argv.includes("--check")) checkStaging();
else packagePlugin();
