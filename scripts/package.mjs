#!/usr/bin/env node
import { execFileSync } from "node:child_process";
import {
  cpSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { crc32, deflateRawSync } from "node:zlib";

const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const manifest = JSON.parse(readFileSync(join(repoRoot, "plugin.json"), "utf8"));
const PLUGIN_DIR_NAME = "LegionGo2Companion";
const version = manifest.version;
const outputIndex = process.argv.indexOf('--output');
if (outputIndex !== -1 && !process.argv[outputIndex + 1]) throw new Error('--output requires a path');
const zipPath = outputIndex === -1 ? join(repoRoot, `${PLUGIN_DIR_NAME}-${version}.zip`) : resolve(process.argv[outputIndex + 1]);

const CONTENTS = [
  "active_app.py",
  "main.py",
  "diagnostics.py",
  "diagnostics_reader.py",
  "desktop_bridge.py",
  "desktop_install.py",
  "desktop",
  "module_control.py",
  "module_runtime.py",
  "conflict_guard.py",
  "safe_settings.py",
  "system_process.py",
  "inputplumber_process.py",
  "companion_updates.py",
  "bounded_http.py",
  "tdp_backend.py",
  "fan_backend.py",
  "fan_ec.py",
  "fan_guardian.py",
  "fan_policy.py",
  "fan_sleep.py",
  "vibration_backend.py",
  "display_backend.py",
  "display_native.py",
  "display_script_io.py",
  "wifi_backend.py",
  "rgb_backend.py",
  "remap_backend.py",
  "battery_backend.py",
  "controller_backend.py",
  "controller_firmware.py",
  "controller_firmware_catalog.py",
  "controller_imu.py",
  "tdp_updater.py",
  "plugin.json",
  "package.json",
  "package-lock.json",
  "rollup.config.js",
  "tsconfig.json",
  "README.md",
  "docs/DESKTOP_UI.md",
  "docs/CONTROLLER_FIRMWARE.md",
  "docs/LOGGING.md",
  "CHANGELOG.md",
  "SOURCE.md",
  "LICENSE",
  "LICENSE.MIT",
  "LICENSE.HUESYNC",
  "LICENSE.DECKY-ROLLUP",
  "NOTICE",
  "requirements.txt",
  "src",
  "scripts/check-build.mjs",
  "scripts/check-desktop-build.mjs",
  "scripts/desktop-contract.mjs",
  "scripts/build-native.py",
  "scripts/verify-package.py",
  "scripts/package.mjs",
  "gamescope",
  "pyudev",
  "dist",
];

const TRACKED = CONTENTS.filter((entry) => entry !== "dist");

function fail(message) {
  console.error(`error: ${message}`);
  process.exit(1);
}

function validatePayload() {
  execFileSync(process.execPath, [join(repoRoot, 'scripts/check-desktop-build.mjs'), '--runtime'], {stdio: 'inherit'});
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

function git(args, options = {}) {
  return execFileSync("git", args, { cwd: repoRoot, encoding: "utf8", ...options });
}

// Tracked files come from the committed tree (git archive), so the checkout's state cannot leak in;
// dist/ is the build output and is not tracked.
function stagePayload(destination, scratch) {
  const source = join(scratch, "source");
  mkdirSync(source, { recursive: true });
  git(["archive", "--format=tar", "-o", join(scratch, "source.tar"), "HEAD", "--",
    ...TRACKED]);
  execFileSync("tar", ["-xf", "../source.tar"], { cwd: source, stdio: "inherit" });
  mkdirSync(destination, { recursive: true });
  for (const entry of CONTENTS) {
    cpSync(join(entry === "dist" ? repoRoot : source, entry), join(destination, entry), {
      recursive: true,
      filter: (src) => !/(__pycache__|\.pyc$|\.DS_Store|node_modules|[\\/]tests(?:[\\/]|$))/.test(src),
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

function listTree(base, prefix = "") {
  const entries = [];
  for (const item of readdirSync(join(base, prefix), { withFileTypes: true })) {
    const name = prefix ? `${prefix}/${item.name}` : item.name;
    if (item.isDirectory()) entries.push(`${name}/`, ...listTree(base, name));
    else if (item.isFile()) entries.push(name);
    else fail(`unsupported payload entry: ${name}`);
  }
  return entries;
}

// Same tree + same timestamp -> byte-identical ZIP: sorted names, one fixed DOS time (UTC),
// fixed modes (0644 files, 0755 directories). Deflate output is fixed for a given Node/zlib.
export function writeZip(base, top, zipPath, epochSeconds) {
  const when = new Date(epochSeconds * 1000);
  const dosTime = (when.getUTCHours() << 11) | (when.getUTCMinutes() << 5) | (when.getUTCSeconds() >> 1);
  const dosDate = ((when.getUTCFullYear() - 1980) << 9) | ((when.getUTCMonth() + 1) << 5) | when.getUTCDate();
  const names = [`${top}/`, ...listTree(base, top)].sort();
  if (names.length > 0xffff) fail("too many ZIP entries");
  const locals = [];
  const central = [];
  let offset = 0;
  for (const name of names) {
    const isDir = name.endsWith("/");
    const data = isDir ? Buffer.alloc(0) : readFileSync(join(base, name));
    const packed = isDir ? data : deflateRawSync(data, { level: 9 });
    const method = !isDir && packed.length < data.length ? 8 : 0;
    const body = method ? packed : data;
    const crc = crc32(data);
    const nameBytes = Buffer.from(name, "utf8");
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(method, 8);
    local.writeUInt16LE(dosTime, 10);
    local.writeUInt16LE(dosDate, 12);
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(body.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    const header = Buffer.alloc(46);
    header.writeUInt32LE(0x02014b50, 0);
    header.writeUInt16LE((3 << 8) | 20, 4);
    header.writeUInt16LE(20, 6);
    header.writeUInt16LE(method, 10);
    header.writeUInt16LE(dosTime, 12);
    header.writeUInt16LE(dosDate, 14);
    header.writeUInt32LE(crc, 16);
    header.writeUInt32LE(body.length, 20);
    header.writeUInt32LE(data.length, 24);
    header.writeUInt16LE(nameBytes.length, 28);
    header.writeUInt32LE((isDir ? 0o40755 : 0o100644) * 0x10000 + (isDir ? 0x10 : 0), 38);
    header.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, body);
    central.push(header, nameBytes);
    offset += local.length + nameBytes.length + body.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(names.length, 8);
  end.writeUInt16LE(names.length, 10);
  end.writeUInt32LE(directory.length, 12);
  end.writeUInt32LE(offset, 16);
  writeFileSync(zipPath, Buffer.concat([...locals, directory, end]));
}

function checkStaging() {
  const root = mkdtempSync(join(tmpdir(), "legiongo2companion-package-check-"));
  try {
    stagePayload(join(root, PLUGIN_DIR_NAME), root);
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
  console.log(`package payload check passed (${CONTENTS.length} entries)`);
}

function packagePlugin() {
  if (outputIndex !== -1 && existsSync(zipPath)) fail('experimental output already exists; choose a fresh file');
  const dirty = git(["status", "--porcelain", "--untracked-files=no", "--", ...TRACKED]).trim();
  if (dirty) fail(`the package is built from HEAD; commit or discard these changes first:\n${dirty}`);
  mkdirSync(dirname(zipPath), {recursive: true});
  rmSync(zipPath, { force: true });
  const root = mkdtempSync(join(tmpdir(), "legiongo2companion-package-"));
  try {
    stagePayload(join(root, "stage", PLUGIN_DIR_NAME), root);
    writeZip(join(root, "stage"), PLUGIN_DIR_NAME, zipPath, Number(git(["log", "-1", "--format=%ct", "HEAD"]).trim()));
  } finally {
    rmSync(root, { recursive: true, force: true });
  }
  console.log(`packaged manifest v${version} from ${git(["rev-parse", "--short", "HEAD"]).trim()} -> ${zipPath}`);
}

if (import.meta.url === pathToFileURL(process.argv[1] ?? "").href) {
  validatePayload();
  if (process.argv.includes("--check")) checkStaging();
  else packagePlugin();
}
