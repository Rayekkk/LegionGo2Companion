#!/usr/bin/env node
// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/build/package.mjs): change it there, then `kit sync`.
/**
 * Builds the release zip that Decky's "Install Plugin from ZIP" accepts: artifacts/<folder>-<version>.zip and its
 * .sha256 (sha256sum format). `--check` runs every check without writing anything; `--release` also refuses
 * uncommitted changes in anything that ships or builds it (for a release ZIP built on the owner's machine).
 *
 * Sources: DeckyLS (one runtime list in package.json "decky", CHANGELOG/tag checks), LegionGo2Companion/
 * Ayaneo3Companion scripts/package.mjs (own ZIP writer, fixed folder, staging outside the repo, symlink refusal,
 * caches and nested tests left out, atomic publish, lockfile identity, unknown arguments refused) and
 * verify-package.py / lego_updater.py (read the finished ZIP back the way the updater does, byte-for-byte with the
 * sources, secret-like names), DeckyVibranceHDR verify_package.py and build.yml (@decky/api version = lockfile,
 * empty and "." path parts, tag builds need a released CHANGELOG section), DeckyEQ scripts/package.py (no module
 * left out by accident, expected plugin flags), FSR4Injector/DeckyReshade (forbidden third-party files).
 *
 * Guarantees:
 * - The ZIP holds one folder, package.json "decky.folder" (default: plugin.json's name) - the folder Decky and the
 *   plugins' own updaters already use - and in it exactly "decky.files" (paths or top-level `*` patterns, each
 *   matching something, never absolute, "..", or with backslashes) plus @decky/api's licence and source under
 *   third_party/decky-api/. __pycache__, .pyc/.pyo, .DS_Store and nested tests/ folders are left out of listed
 *   folders. A top-level .py file that no entry lists fails the build unless "decky.exclude" names it.
 * - CHANGELOG style (STANDARD §7): inside each version only the headings "### ✨ Added", "### 🔧 Changed",
 *   "### 🐛 Fixed", "### 🗑️ Removed", in that order, each at most once; no other "###"/"####" headings; inside a
 *   category the entries of one area stand together.
 * - Versions agree: plugin.json, package.json, package-lock.json (root and packages[""]), the newest CHANGELOG
 *   release section, and on a CI tag build the tag, with no [Unreleased] section above it; package and lockfile
 *   have the same name and licence; the shipped @decky/api is the version the lockfile pins; with "decky.flags"
 *   given, plugin.json's flags must be exactly those.
 * - Payload: regular files and folders only (a symlink, FIFO or device anywhere is refused); never node_modules,
 *   .git, .github, .venv/venv, settings.json(.bak), session.json, .env*, .pem, .key or .log; nothing matching
 *   "decky.forbidden".
 * - The ZIP is byte-identical for the same files on any OS with the same Node: own writer, sorted names, one fixed
 *   time, fixed modes (0644 files, 0755 folders), UTF-8 names.
 * - The finished ZIP is read back before it is published: entry count and sizes within limits (inflation bounded
 *   while unpacking), every name safe (no absolute, empty, ".", "..", backslash, ":" or control character part),
 *   no duplicate even by letter case, no file and folder at one path, nothing encrypted, local and central headers
 *   agree, every CRC right and every file byte-for-byte equal to its source in the repo or node_modules.
 * - Staging happens in a temporary folder outside the repo that is always removed; the new ZIP and .sha256 replace
 *   the old ones only when everything passed, the old .sha256 going first so a pair never mismatches.
 * - dist/ is checked by the plugin's scripts/check-build.mjs first, so `npm run package` alone can never ship a
 *   bundle older than src/ (DeckyVibranceHDR verify_package.py).
 * - In the plugin's own git checkout, a listed folder that git tracks ships only what git tracks: an untracked or
 *   ignored file in it (a module never added, a stray note) or a tracked file deleted from it fails every build,
 *   not only a release (DeckyEQ scripts/package.py). Untracked folders (dist/) and listed single files (a built
 *   binary) are the plugin's to vouch for.
 * - check-build.mjs gets two minutes at most; a bundle that never lets it end fails instead of hanging.
 * - With --release: `git status` must show no change (nor untracked file) in decky.files, plugin.json,
 *   package(-lock).json, CHANGELOG.md, src/, scripts/, rollup.config.js and tsconfig.json; no git = refused
 *   (DeckyVibranceHDR verify_package.py).
 * - Unknown arguments are refused; imported as a module (for tests), nothing runs.
 *
 * Deliberately not here: building from `git archive` or refusing a dirty tree by default (DeckyKit's `deploy`
 * installs the working tree on test devices; --release opts in), plugin-specific binary checks
 * (FSR4Injector's shim, DeckyEQ's ELF stamp) and checks against old shipped updaters (a plugin's own
 * scripts/verify-package.py, which CI runs when it exists).
 */

import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";
import {
  cpSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { basename, dirname, isAbsolute, join, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { crc32, deflateRawSync, inflateRawSync } from "node:zlib";

const MAX_ENTRIES = 1024;
const MAX_ZIP_BYTES = 32 * 1024 * 1024;
const MAX_UNPACKED_BYTES = 128 * 1024 * 1024;
const FIXED_TIME = Date.UTC(2000, 0, 1) / 1000;
const SKIPPED = name => ["__pycache__", ".DS_Store"].includes(name) || /\.py[co]$/.test(name);
const ALWAYS_FORBIDDEN = [/(^|\/)node_modules(\/|$)/, /(^|\/)\.git(\/|$)/, /(^|\/)\.github(\/|$)/,
  /(^|\/)\.?venv(\/|$)/, /(^|\/)settings\.json(\.bak)?$/, /(^|\/)session\.json$/, /(^|\/)\.env/, /\.(pem|key|log)$/i,
  /(^|\/)__pycache__(\/|$)/, /\.py[co]$/];
const ARGS = new Set(["--check", "--release"]);

class PackageError extends Error {}
const fail = message => { throw new PackageError(message); };

const CATEGORIES = ["### ✨ Added", "### 🔧 Changed", "### 🐛 Fixed", "### 🗑️ Removed"];

/** STANDARD §7: each version holds only the four category headings, in order, each at most once. Inside a category
 * the entries of one area stand together: below 5 entries as "- **Fan:** …" bullets, from 5 entries under one
 * "**Fan**" line per area with plain "- …" bullets. */
export function checkChangelogStyle(text) {
  const SHORT = /^- \*\*([^*]+?):\*\*/, HEADER = /^\*\*([^*]+)\*\*$/;
  let version = null, last = -1, category = null, areas = [], headers = 0, bullets = 0, plain = 0;
  const close = () => {
    if (category === null) return;
    const where = `CHANGELOG ${version} ${category}`;
    if (bullets >= 5 && !headers) fail(`${where}: ${bullets} entries need "**Area**" lines above each area's bullets`);
    if (bullets < 5 && headers) fail(`${where}: under 5 entries use "- **Area:** …" bullets, not "**Area**" lines`);
    if (headers && plain !== bullets) fail(`${where}: under "**Area**" lines the bullets do not repeat the area`);
    if (!headers && plain) fail(`${where}: every entry starts with "- **Area:**"`);
  };
  for (const line of text.split(/\r?\n/)) {
    if (line.startsWith("## ")) { close(); category = null; version = line; last = -1; continue; }
    if (line.startsWith("###")) {
      close();
      const at = CATEGORIES.indexOf(line.trimEnd());
      if (version === null || at < 0) fail(`CHANGELOG heading "${line}" is not one of: ${CATEGORIES.join(", ")}`);
      if (at <= last) fail(`CHANGELOG ${version}: "${line}" is out of order or repeated (${CATEGORIES.join(" > ")})`);
      last = at; category = line.trim(); areas = []; headers = bullets = plain = 0;
      continue;
    }
    if (category === null) continue;
    const area = (HEADER.exec(line) ?? SHORT.exec(line))?.[1].trim().toLowerCase();
    if (HEADER.test(line)) headers++;
    if (line.startsWith("- ")) { bullets++; if (!SHORT.test(line)) plain++; }
    if (area === undefined) continue;
    if (areas.includes(area) && (areas.at(-1) !== area || HEADER.test(line)))
      fail(`CHANGELOG ${version}: "${area}" entries are split up; keep one area's entries together`);
    if (areas.at(-1) !== area) areas.push(area);
  }
  close();
}

/** Every entry under base/prefix ("dir/" for folders), refusing anything but regular files and folders. */
function listTree(base, prefix) {
  const entries = [];
  for (const item of readdirSync(join(base, prefix), { withFileTypes: true }).sort((a, b) => a.name < b.name ? -1 : 1)) {
    const name = `${prefix}/${item.name}`;
    if (item.isDirectory()) entries.push(`${name}/`, ...listTree(base, name));
    else if (item.isFile()) entries.push(name);
    else fail(`not a regular file or folder (symlink?): ${name}`);
  }
  return entries;
}

/** Untracked, ignored or deleted files inside the listed folders that git tracks; [] outside the plugin's own
 * checkout (a copy without git, or a folder inside another repository). */
function untrackedInListedFolders(repoRoot, contents) {
  const git = (...gitArgs) => execFileSync("git", gitArgs, { cwd: repoRoot, encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"], maxBuffer: 64 << 20 });
  try {
    const top = real(git("rev-parse", "--show-toplevel").trim()), own = real(repoRoot);
    if (process.platform === "win32" ? top.toLowerCase() !== own.toLowerCase() : top !== own) return [];
  } catch {
    return [];  // no git, or not a checkout: the plugin vouches for its files (a copy deployed from elsewhere)
  }
  const staged = path => !path.split("/").some((part, i, parts) => SKIPPED(part) || (part === "tests" && i < parts.length - 1));
  try {
    const folders = contents.filter(entry => existsSync(join(repoRoot, entry))
      && lstatSync(join(repoRoot, entry)).isDirectory() && git("ls-files", "-z", "--", entry));
    if (!folders.length) return [];
    const list = (...flags) => git("ls-files", "-z", ...flags, "--", ...folders).split("\0").filter(Boolean);
    return [...list("--others"), ...list("--deleted")].filter(staged).sort();
  } catch (e) {
    return fail(`git could not list the shipped folders: ${String(e.stderr || e.message).trim().split("\n")[0]}`);
  }
}

/** Copy from -> to (file or folder), leaving caches and nested tests/ out; returns [staged path, source] pairs. */
function stageFrom(from, to) {
  const info = lstatSync(from);
  if (info.isSymbolicLink() || !(info.isFile() || info.isDirectory())) fail(`not a regular file or folder: ${from}`);
  if (info.isFile()) {
    mkdirSync(dirname(to), { recursive: true });
    cpSync(from, to);
    return [[to, from]];
  }
  mkdirSync(to, { recursive: true });
  const pairs = [];
  for (const item of readdirSync(from).sort()) {
    if (SKIPPED(item) || item === "tests") continue;  // a listed folder's own tests stay home
    pairs.push(...stageFrom(join(from, item), join(to, item)));
  }
  return pairs;
}

/** Same tree -> same bytes: sorted names, one DOS time (UTC), fixed modes, deflate only when it helps. */
export function writeZip(base, top, epochSeconds = FIXED_TIME) {
  const when = new Date(epochSeconds * 1000);
  const dosTime = (when.getUTCHours() << 11) | (when.getUTCMinutes() << 5) | (when.getUTCSeconds() >> 1);
  const dosDate = ((when.getUTCFullYear() - 1980) << 9) | ((when.getUTCMonth() + 1) << 5) | when.getUTCDate();
  const names = [`${top}/`, ...listTree(base, top)].sort();
  if (names.length > MAX_ENTRIES) fail(`too many ZIP entries (${names.length} > ${MAX_ENTRIES})`);
  const locals = [], central = [];
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
    local.writeUInt32LE(0x04034b50, 0); local.writeUInt16LE(20, 4); local.writeUInt16LE(0x0800, 6);
    local.writeUInt16LE(method, 8); local.writeUInt16LE(dosTime, 10); local.writeUInt16LE(dosDate, 12);
    local.writeUInt32LE(crc, 14); local.writeUInt32LE(body.length, 18); local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    const header = Buffer.alloc(46);
    header.writeUInt32LE(0x02014b50, 0); header.writeUInt16LE((3 << 8) | 20, 4); header.writeUInt16LE(20, 6);
    header.writeUInt16LE(0x0800, 8); header.writeUInt16LE(method, 10); header.writeUInt16LE(dosTime, 12);
    header.writeUInt16LE(dosDate, 14); header.writeUInt32LE(crc, 16); header.writeUInt32LE(body.length, 20);
    header.writeUInt32LE(data.length, 24); header.writeUInt16LE(nameBytes.length, 28);
    header.writeUInt32LE(((isDir ? 0o40755 : 0o100644) * 0x10000 + (isDir ? 0x10 : 0)) >>> 0, 38);
    header.writeUInt32LE(offset, 42);
    locals.push(local, nameBytes, body);
    central.push(header, nameBytes);
    offset += local.length + nameBytes.length + body.length;
  }
  const directory = Buffer.concat(central);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0); end.writeUInt16LE(names.length, 8); end.writeUInt16LE(names.length, 10);
  end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  return Buffer.concat([...locals, directory, end]);
}

/** [{name, data}] of a ZIP, every entry checked like the updaters check it; refuses anything this writer never makes. */
export function readZip(zip) {
  if (zip.length > MAX_ZIP_BYTES) fail(`ZIP larger than ${MAX_ZIP_BYTES} bytes`);
  const eocd = zip.lastIndexOf(Buffer.from([0x50, 0x4b, 0x05, 0x06]));
  if (eocd < 0 || eocd + 22 > zip.length) fail("ZIP has no end-of-directory record");
  const count = zip.readUInt16LE(eocd + 10);
  if (count > MAX_ENTRIES) fail(`too many ZIP entries (${count})`);
  let at = zip.readUInt32LE(eocd + 16), unpacked = 0;
  const entries = [], seen = new Set(), files = new Set(), folders = new Set();
  for (let i = 0; i < count; i++) {
    if (at + 46 > zip.length || zip.readUInt32LE(at) !== 0x02014b50) fail("broken ZIP central directory");
    const flags = zip.readUInt16LE(at + 8), method = zip.readUInt16LE(at + 10), crc = zip.readUInt32LE(at + 16);
    const packedSize = zip.readUInt32LE(at + 20), size = zip.readUInt32LE(at + 24);
    const nameLength = zip.readUInt16LE(at + 28), extra = zip.readUInt16LE(at + 30), comment = zip.readUInt16LE(at + 32);
    const local = zip.readUInt32LE(at + 42);
    const rawName = zip.subarray(at + 46, at + 46 + nameLength);
    const name = rawName.toString("utf8");
    at += 46 + nameLength + extra + comment;
    if (flags & 0x1) fail(`encrypted ZIP entry: ${name}`);
    const parts = name.replace(/\/$/, "").split("/");
    if (!name || name.startsWith("/") || /[\\:\x00-\x1f\x7f]/.test(name) || parts.some(p => p === "" || p === "." || p === ".."))
      fail(`unsafe ZIP path: ${JSON.stringify(name)}`);
    const folded = name.toLowerCase();
    if (seen.has(folded)) fail(`duplicate ZIP entry: ${name}`);
    seen.add(folded);
    const path = folded.replace(/\/$/, "");
    if (name.endsWith("/") ? files.has(path) : folders.has(path)) fail(`file and folder at one path: ${name}`);
    (name.endsWith("/") ? folders : files).add(path);
    for (let j = 1; j < parts.length; j++)
      if (files.has(parts.slice(0, j).join("/").toLowerCase())) fail(`file and folder at one path: ${name}`);
    unpacked += size;
    if (unpacked > MAX_UNPACKED_BYTES) fail(`ZIP unpacks to more than ${MAX_UNPACKED_BYTES} bytes`);
    if (local + 30 > zip.length || zip.readUInt32LE(local) !== 0x04034b50) fail(`broken ZIP entry: ${name}`);
    const localNameLength = zip.readUInt16LE(local + 26);
    if (zip.readUInt16LE(local + 8) !== method || zip.readUInt32LE(local + 14) !== crc
        || !zip.subarray(local + 30, local + 30 + localNameLength).equals(rawName))
      fail(`local and central ZIP headers disagree: ${name}`);
    const start = local + 30 + localNameLength + zip.readUInt16LE(local + 28);
    const body = zip.subarray(start, start + packedSize);
    let data;
    try {
      data = method === 8 ? inflateRawSync(body, { maxOutputLength: Math.max(size, 1) })
        : method === 0 ? Buffer.from(body) : fail(`unknown compression method: ${name}`);
    } catch (e) {
      if (e instanceof PackageError) throw e;
      fail(`ZIP entry cannot be unpacked within its size: ${name}`);
    }
    if (data.length !== size || crc32(data) !== crc) fail(`ZIP entry fails its CRC: ${name}`);
    entries.push({ name, data });
  }
  return entries;
}

function main(args) {
  for (const arg of args) if (!ARGS.has(arg)) fail(`unknown argument: ${arg} (allowed: ${[...ARGS].join(" ")})`);
  const checkOnly = args.includes("--check");
  const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
  const json = file => JSON.parse(readFileSync(join(repoRoot, file), "utf8"));
  const manifest = json("plugin.json"), pkg = json("package.json"), lock = json("package-lock.json");
  const version = manifest.version;
  const config = pkg.decky ?? fail('package.json has no "decky" section (files, forbidden)');
  const folder = config.folder ?? manifest.name;
  if (typeof folder !== "string" || !/^[A-Za-z0-9][A-Za-z0-9 ._-]*$/.test(folder)) fail(`unusable folder name: ${folder}`);
  if (!/^\d+\.\d+\.\d+$/.test(version ?? "")) fail(`plugin.json version is not X.Y.Z: ${version}`);
  if (!Array.isArray(config.files) || !config.files.length) fail('package.json "decky.files" must list the runtime files');
  if (config.flags !== undefined && JSON.stringify(manifest.flags ?? []) !== JSON.stringify(config.flags))
    fail(`plugin.json flags ${JSON.stringify(manifest.flags)} differ from decky.flags ${JSON.stringify(config.flags)}`);

  for (const [where, found] of [["package.json", pkg.version], ["package-lock.json", lock.version],
    ['package-lock.json packages[""]', lock.packages?.[""]?.version]])
    if (found !== version) fail(`version mismatch: plugin.json=${version} ${where}=${found}`);
  for (const [where, found] of [["package-lock.json", lock.name], ['package-lock.json packages[""]', lock.packages?.[""]?.name]])
    if (found !== pkg.name) fail(`${where} name ${found} != package.json ${pkg.name}`);
  if (lock.packages?.[""]?.license !== pkg.license) fail(`package-lock.json licence ${lock.packages?.[""]?.license} != ${pkg.license}`);
  const changelog = readFileSync(join(repoRoot, "CHANGELOG.md"), "utf8");
  checkChangelogStyle(changelog);
  const sections = [...changelog.matchAll(/^## \[([^\]]+)\]/gm)].map(m => m[1]);
  const released = sections.find(s => /^\d+\.\d+\.\d+$/.test(s));
  if (released !== version) fail(`version mismatch: plugin.json=${version}, newest CHANGELOG release=${released}`);
  if (process.env.GITHUB_REF_TYPE === "tag") {
    if (process.env.GITHUB_REF_NAME !== version) fail(`version mismatch: tag=${process.env.GITHUB_REF_NAME} plugin.json=${version}`);
    if (sections[0] !== version) fail(`a tag build needs CHANGELOG's top section to be [${version}], not [${sections[0]}]`);
  }
  if (!existsSync(join(repoRoot, "dist", "index.js"))) fail("dist/index.js is missing - run `npm run build` first");
  const checkBuild = join(repoRoot, "scripts", "check-build.mjs");
  if (!existsSync(checkBuild)) fail("scripts/check-build.mjs is missing (kit sync)");
  try {
    execFileSync(process.execPath, [checkBuild], { cwd: repoRoot, stdio: "pipe", timeout: 120_000 });
  } catch (e) {
    const why = String(e.stderr ?? "").split("\n").find(line => /Error/.test(line)) ?? e.message;
    fail(`dist/ fails scripts/check-build.mjs (run \`npm run build\`): ${why.trim()}`);
  }

  const contents = config.files.flatMap(entry => {
    if (typeof entry !== "string" || !entry || isAbsolute(entry) || entry.includes("\\") || entry.split("/").includes(".."))
      fail(`decky.files: not a plain relative path: ${entry}`);
    if (!entry.includes("*")) return [entry];
    if (entry.includes("/")) fail(`decky.files: patterns work in the top folder only: ${entry}`);
    const pattern = new RegExp(`^${entry.replace(/[.+?^${}()|[\]\\]/g, "\\$&").replaceAll("*", "[^/]*")}$`);
    const hits = readdirSync(repoRoot).filter(file => pattern.test(file)).sort();
    if (!hits.length) fail(`decky.files: nothing matches ${entry}`);
    return hits;
  });
  for (const file of readdirSync(repoRoot).filter(f => f.endsWith(".py")))
    if (!contents.includes(file) && !(config.exclude ?? []).includes(file))
      fail(`${file} is not packaged: add it to decky.files, or to decky.exclude when that is intended`);
  const forbidden = [...ALWAYS_FORBIDDEN, ...(config.forbidden ?? []).map(r => new RegExp(r, "i"))];
  const strays = untrackedInListedFolders(repoRoot, contents);
  if (strays.length) fail(`not in git (git add it, or remove it): ${strays.slice(0, 5).join(", ")}`);
  if (args.includes("--release")) {
    const inputs = [...new Set([...contents, "plugin.json", "package.json", "package-lock.json", "CHANGELOG.md", "src",
      "scripts", "rollup.config.js", "tsconfig.json"])].filter(path => existsSync(join(repoRoot, path)));
    const git = (...gitArgs) => execFileSync("git", gitArgs, { cwd: repoRoot, encoding: "utf8", stdio: ["ignore", "pipe", "pipe"] });
    let changed;
    try {
      // The plugin's own repository: a folder another repository ignores would show no change at all.
      const [top, own] = [real(git("rev-parse", "--show-toplevel").trim()), real(repoRoot)];
      if (process.platform === "win32" ? top.toLowerCase() !== own.toLowerCase() : top !== own) fail("--release needs the plugin's own git checkout");
      changed = git("status", "--porcelain", "--untracked-files=all", "--", ...inputs);
    } catch (e) {
      if (e instanceof PackageError) throw e;
      fail(`--release needs the plugin's own git checkout: ${String(e.stderr || e.message).trim()}`);
    }
    if (changed.trim()) fail(`--release: uncommitted changes in shipped files:\n${changed.trimEnd()}`);
  }

  const apiDir = join(repoRoot, "node_modules", "@decky", "api");
  const apiVersion = JSON.parse(readFileSync(join(apiDir, "package.json"), "utf8")).version;
  const lockedApi = lock.packages?.["node_modules/@decky/api"]?.version;
  if (apiVersion !== lockedApi) fail(`@decky/api ${apiVersion} installed, lockfile pins ${lockedApi}: run npm ci`);

  const stage = mkdtempSync(join(tmpdir(), "deckykit-package-"));
  try {
    const top = join(stage, folder);
    const sources = new Map();
    const record = pairs => { for (const [to, from] of pairs) sources.set(relative(stage, to).replace(/\\/g, "/"), from); };
    for (const entry of contents) {
      if (!existsSync(join(repoRoot, entry))) fail(`required file missing: ${entry}`);
      record(stageFrom(join(repoRoot, entry), join(top, entry)));
    }
    for (const entry of ["LICENSE", "package.json", "dist", "src", "README.md"]) {
      if (existsSync(join(apiDir, entry))) record(stageFrom(join(apiDir, entry), join(top, "third_party", "decky-api", entry)));
      else if (entry !== "README.md") fail(`@decky/api ${entry} is missing`);
    }
    const payload = listTree(stage, folder);
    for (const path of payload)
      for (const rule of forbidden) if (rule.test(path.slice(folder.length + 1))) fail(`archive must not contain ${path}`);

    const zip = writeZip(stage, folder);
    const entries = readZip(zip);
    const missing = new Set(sources.keys());
    for (const { name: entry, data } of entries) {
      if (entry !== `${folder}/` && !entry.startsWith(`${folder}/`)) fail(`archive entry outside the plugin folder: ${entry}`);
      if (entry.endsWith("/")) continue;
      const from = sources.get(entry) ?? fail(`archive has a file with no source: ${entry}`);
      if (!data.equals(readFileSync(from))) fail(`archive differs from its source ${relative(repoRoot, from)}`);
      missing.delete(entry);
    }
    if (missing.size) fail(`archive is missing ${[...missing][0]}`);
    for (const required of [...contents.filter(e => lstatSync(join(repoRoot, e)).isFile()), "dist/index.js",
      "third_party/decky-api/LICENSE"])
      if (!entries.some(e => e.name === `${folder}/${required}`)) fail(`archive is missing ${required}`);

    const sha256 = createHash("sha256").update(zip).digest("hex");
    if (checkOnly) {
      console.log(`check ok: v${version}, ${entries.length} entries, sha256 ${sha256} (nothing written)`);
      return;
    }
    const outDir = join(repoRoot, "artifacts");
    const zipPath = join(outDir, `${folder}-${version}.zip`);
    mkdirSync(outDir, { recursive: true });
    const pending = `${zipPath}.pending-${process.pid}`;
    try {
      writeFileSync(pending, zip);
      writeFileSync(`${pending}.sha256`, `${sha256}  ${basename(zipPath)}\n`);
      rmSync(`${zipPath}.sha256`, { force: true });  // never a new ZIP beside the old checksum
      renameSync(pending, zipPath);
      renameSync(`${pending}.sha256`, `${zipPath}.sha256`);
    } finally {
      rmSync(pending, { force: true });
      rmSync(`${pending}.sha256`, { force: true });
    }
    console.log(`packaged v${version} (${entries.length} entries) -> ${relative(repoRoot, zipPath)}\nsha256 ${sha256}`);
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

const real = path => { try { return realpathSync(path); } catch { return resolve(path); } };
const self = real(fileURLToPath(import.meta.url));
const invoked = process.argv[1] ? real(process.argv[1]) : "";
if (process.platform === "win32" ? invoked.toLowerCase() === self.toLowerCase() : invoked === self) {
  try {
    main(process.argv.slice(2));
  } catch (e) {
    console.error(`error: ${e instanceof PackageError ? e.message : e.stack}`);
    process.exitCode = 1;
  }
}
