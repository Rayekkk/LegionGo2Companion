// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

// The main page's summaries: one read per module, kept across QAM remounts, polled only while the list is shown.
import { overviewSummary, type RunningLimits } from "../desktop/native/tdp.mjs";
import { getFanState, type FanState } from "./fan";
import { getVibrationSettings } from "./vibration";
import { getWifiStatus, type WifiStatus } from "./wifi";
import { getRgbStatus, type RgbStatus } from "./rgb";
import { getRemapStatus, type RemapStatus } from "./remap";
import { getBatteryStatus, getControllerBatteryLevels, type BatteryStatus, type ControllerBatteryStatus } from "./battery";
import { getControllerStatus, type ControllerStatus } from "./controller";
import { rpc } from "./shared";
import { moduleShown } from "../desktop/native/modules.mjs";

export type ModuleKey = "tdp" | "fan" | "vibration" | "display" | "wifi" | "rgb" | "remap" | "battery" | "controller";
export type ModuleStates = Partial<Record<ModuleKey, {enabled: boolean; applicable?: boolean | null; ready?: boolean; pending?: boolean; maintenance?: boolean; starting?: boolean; error?: string; note?: string}>>;
export const EMPTY_MODULES: ModuleStates = {};
export const moduleEnabled = (modules: ModuleStates, key: ModuleKey) => moduleShown(modules[key]) && modules[key]?.enabled !== false && modules[key]?.ready !== false && !modules[key]?.pending;

interface TdpSettings {
  enabled?: boolean;
  active_preset?: string;
  active_spl?: number;
  active_sppt?: number;
  active_fppt?: number;
  running?: RunningLimits;
  spl?: number;
  sppt?: number;
  fppt?: number;
}

interface VibeSettingsResponse {
  settings?: { level?: number; mode?: number; touchpadEnabled?: boolean };
}

interface DriverStatus { found?: boolean }

interface DisplayState {
  steamos_update_needed?: boolean;
  edid_fix?: boolean;
  edid_patched?: boolean;
  settings_error?: string;
}

export interface Overview {
  version: string;
  reads?: Partial<Record<OverviewField, { unavailable: boolean; note?: string }>>;
  tdp?: TdpSettings;
  fan?: FanState;
  vibration?: VibeSettingsResponse;
  driver?: DriverStatus;
  display?: DisplayState;
  wifi?: WifiStatus;
  rgb?: RgbStatus;
  remap?: RemapStatus;
  battery?: BatteryStatus;
  controller_batteries?: ControllerBatteryStatus;
  controller?: ControllerStatus;
}

// Injected by rollup from package.json; shown until the backend reports its own version.
declare const __PLUGIN_VERSION__: string;
export const PLUGIN_VERSION = __PLUGIN_VERSION__;
const getTdpSettings = rpc<[], TdpSettings>("get_settings");
const getDriverStatus = rpc<[], DriverStatus>("vibe_get_driver_status");
const getDisplayState = rpc<[], DisplayState>("display_get_state");

// Keep confirmed summaries and in-flight reads across QAM remounts. Each source
// publishes independently, so a slow hardware/network probe cannot hide ready data.
type OverviewField = Exclude<keyof Overview, "version" | "reads">;
const OVERVIEW_DELAY_MS = 30000;
interface OverviewSource {
  key: OverviewField;
  module: ModuleKey;
  read: () => Promise<Partial<Overview>>;
  revision: number;
  pending: Promise<Partial<Overview>> | null;
  lastReadAt: number | null;
  failures: number;
  pendingSince: number | null;
}
let overviewActive = false; // the plugin is loaded (definePlugin until onDismount)
const overviewSources: OverviewSource[] = [
  { key: "tdp", module: "tdp", read: async () => ({ tdp: await getTdpSettings() }) },
  { key: "fan", module: "fan", read: async () => ({ fan: await getFanState(() =>
    overviewActive && !overviewPaused && !!overviewPollers.size && [...overviewViewChecks].some(check => check())) }) },
  { key: "vibration", module: "vibration", read: async () => ({ vibration: await getVibrationSettings() }) },
  { key: "driver", module: "vibration", read: async () => ({ driver: await getDriverStatus() }) },
  { key: "display", module: "display", read: async () => ({ display: await getDisplayState() }) },
  { key: "wifi", module: "wifi", read: async () => ({ wifi: await getWifiStatus() }) },
  { key: "rgb", module: "rgb", read: async () => ({ rgb: await getRgbStatus() }) },
  { key: "remap", module: "remap", read: async () => ({ remap: await getRemapStatus() }) },
  { key: "battery", module: "battery", read: async () => ({ battery: await getBatteryStatus() }) },
  { key: "controller_batteries", module: "battery",
    read: async () => ({ controller_batteries: await getControllerBatteryLevels() }) },
  { key: "controller", module: "controller", read: async () => ({ controller: await getControllerStatus() }) },
].map(source => ({ ...source, revision: 0, pending: null,
  lastReadAt: null, failures: 0, pendingSince: null } as OverviewSource));
let overviewCache: Overview = { version: PLUGIN_VERSION };
let overviewModules = EMPTY_MODULES;
let overviewPaused = true;
const overviewListeners = new Set<(overview: Overview) => void>();
const overviewPollers = new Set<(overview: Overview) => void>();
const overviewViewChecks = new Set<() => boolean>();
let overviewVisibleStarted: number | null = null;
let overviewVisibleSpent = 0;
const overviewVisibleTime = () => overviewVisibleSpent + (overviewVisibleStarted === null
  ? 0 : Math.max(0, Date.now() - overviewVisibleStarted));
const readAge = (milliseconds: number) => {
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return seconds < 60 ? `${seconds}s` : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h`;
};
export const currentOverview = () => overviewCache;
const publishOverview = () => {
  const now = Date.now(), visibleTime = overviewVisibleTime();
  const reads: Overview["reads"] = {};
  for (const source of overviewSources) {
    const delayed = source.pendingSince !== null && visibleTime - source.pendingSince >= OVERVIEW_DELAY_MS;
    const unavailable = source.lastReadAt === null && (source.failures > 0 || delayed);
    let note: string | undefined;
    if (unavailable) note = delayed ? "Still waiting for an update. Check the Decky connection if this continues."
      : "Retrying while this menu is open.";
    else if (source.lastReadAt !== null) {
      const age = readAge(now - source.lastReadAt);
      if (source.failures >= 2 || delayed) note = `Older data · Last read ${age} ago${delayed ? " · Update delayed" : " · Retrying…"}`;
      else if (source.pendingSince !== null && now - source.lastReadAt >= OVERVIEW_DELAY_MS)
        note = `Updating… · Last read ${age} ago`;
    }
    reads[source.key] = { unavailable, note };
  }
  overviewCache = { ...overviewCache, reads };
  overviewListeners.forEach(listener => listener(overviewCache));
};
export const invalidateOverviewReads = () => {
  overviewSources.forEach(source => { source.revision += 1; source.failures = 0; source.pendingSince = null; });
  overviewCache = { ...overviewCache, reads: {} };
};
const clearOverview = () => {
  invalidateOverviewReads();
  overviewSources.forEach(source => { source.lastReadAt = null; });
  overviewCache = { version: PLUGIN_VERSION };
};
export const configureOverviewModules = (modules: ModuleStates) => {
  for (const source of overviewSources) {
    if (moduleEnabled(overviewModules, source.module) !== moduleEnabled(modules, source.module)) {
      source.revision += 1;
      source.lastReadAt = null; source.failures = 0; source.pendingSince = null;
      overviewCache = { ...overviewCache, [source.key]: undefined,
        reads: { ...overviewCache.reads, [source.key]: undefined } };
    }
  }
  overviewModules = modules;
};
const readOverviewSource = (source: OverviewSource) => {
  if (!overviewActive || overviewPaused || !overviewPollers.size ||
      !moduleEnabled(overviewModules, source.module)
      || (source.key === "controller_batteries" && !moduleShown(overviewModules.controller))) return;
  if (source.pendingSince === null) source.pendingSince = overviewVisibleTime();
  if (source.pending) return;
  const revision = source.revision;
  const request = source.read();
  source.pending = request;
  void request.then(value => {
    if (!overviewActive || overviewPaused || revision !== source.revision) return;
    if (!value[source.key] || typeof value[source.key] !== "object" || Array.isArray(value[source.key]))
      throw new Error("Status unavailable");
    source.lastReadAt = Date.now(); source.failures = 0; source.pendingSince = null;
    overviewCache = { ...overviewCache, ...value };
    publishOverview();
  }).catch(() => {
    if (!overviewActive || overviewPaused || revision !== source.revision) return;
    source.pendingSince = null;
    // Hidden time and hidden failures are not evidence of a failing module.
    // Keep its last report, including any hardware warning returned with it.
    if (overviewPollers.size) {
      source.failures += 1;
      if (source.key === "fan" && overviewCache.fan) overviewCache = { ...overviewCache,
        fan: { ...overviewCache.fan, readings: { valid: false, error: "Fresh fan reading unavailable." } } };
      publishOverview();
    }
  }).finally(() => {
    if (source.pending !== request) return;
    source.pending = null;
    // A page edit or module transition can invalidate an earlier read. If the
    // overview is visible again, refresh that source without waiting for a timer.
    if (revision !== source.revision) readOverviewSource(source);
  });
};
const refreshOverview = () => {
  if (!overviewActive || overviewPaused || !overviewPollers.size) return;
  overviewSources.forEach(readOverviewSource);
  publishOverview();
};

/** A plugin load (active) or unload: the previous session's summaries and readers are gone. */
export const resetOverview = (active: boolean) => {
  overviewActive = active;
  clearOverview();
  overviewPaused = true;
  overviewListeners.clear();
  overviewPollers.clear();
  overviewVisibleStarted = null; overviewVisibleSpent = 0;
  if (active) { overviewModules = EMPTY_MODULES; overviewSources.forEach(source => { source.pending = null; }); }
};
/** No verified compatibility status: stop reading and forget the summaries. */
export const pauseOverview = () => { overviewPaused = true; clearOverview(); };
/** A verified compatibility status: read only while not blocked, only the enabled modules. */
export const guardOverview = (blocked: boolean, modules: ModuleStates, version: string) => {
  overviewPaused = blocked;
  if (blocked) clearOverview();
  configureOverviewModules(modules);
  overviewCache = { ...overviewCache, version };
  publishOverview();
};
/** Keep a mounted view in sync with the cache (an already-running read may finish while QAM is hidden). */
export const watchOverview = (listener: (overview: Overview) => void) => {
  overviewListeners.add(listener);
  return () => { overviewListeners.delete(listener); };
};
/** Poll while the list is on screen (every 10 s, the fan every 1 s); the returned function stops it. */
export const pollOverview = (listener: (overview: Overview) => void, onScreen: () => boolean, fan: boolean) => {
  if (!overviewPollers.size) overviewVisibleStarted = Date.now();
  overviewPollers.add(listener);
  overviewViewChecks.add(onScreen);
  listener(overviewCache);
  refreshOverview();
  const timer = setInterval(refreshOverview, 10000);
  const fanSource = overviewSources.find(source => source.key === "fan")!;
  const fanTimer = fan ? setInterval(() => {
    readOverviewSource(fanSource); publishOverview();
  }, 1000) : undefined;
  return () => {
    clearInterval(timer); if (fanTimer !== undefined) clearInterval(fanTimer); overviewPollers.delete(listener);
    overviewViewChecks.delete(onScreen);
    if (!overviewPollers.size) { overviewVisibleSpent = overviewVisibleTime(); overviewVisibleStarted = null; }
  };
};

export const overviewDescription = (overview: Overview, module: ModuleKey, description: string) => {
  if (module === "fan") return description; // Keep measured numbers separate from fault status.
  const sources = overviewSources.filter(source => source.module === module);
  if (module === "display" && overview.display?.settings_error) return overview.display.settings_error;
  // A returned error/recovery state is a fresh report, not a rejected RPC.
  // Never replace it with an earlier healthy cache or a generic retry message.
  for (const source of sources) {
    if (source.key === "controller_batteries") continue;
    const value = overview[source.key] as { error?: string; reason?: string; message?: string;
      supported?: boolean; available?: boolean; recovery_pending?: boolean; recovery_required?: boolean } | undefined;
    if (value?.error) return (module === "wifi" && value.message) || value.error;
    if (value?.recovery_pending || value?.recovery_required) return value.reason
      || ((module === "battery" || module === "controller") && description) || "An interrupted change needs recovery";
    if (value?.supported === false || value?.available === false)
      return value.reason || value.message || description || "Status unavailable";
  }
  if (module === "vibration" && overview.driver?.found === false) return description;
  return sources.some(source => source.key !== "controller_batteries"
    && overview.reads?.[source.key]?.unavailable) ? "Status unavailable" : description;
};
export const overviewNote = (overview: Overview, module: ModuleKey) => overviewSources
  .filter(source => source.module === module).map(source => overview.reads?.[source.key]?.note).filter(Boolean).join(" · ") || undefined;

export const tdpSummary = (settings?: TdpSettings) => settings ? overviewSummary(settings) : "Power limits and CPU settings";

const VIBE_LEVELS = ["Off", "Low", "Medium", "High"];
const VIBE_MODES = ["FPS", "Racing", "Standard", "SPG", "RPG"];

/** goS: the Legion Go S beta has no vibration modes. */
export const vibrationSummary = (overview: Overview, goS = false) => {
  if (overview.driver && !overview.driver.found) return "Controller driver not detected";
  const settings = overview.vibration?.settings;
  if (!settings) return "Handles, touchpad and per-game profiles";
  const touchpad = settings.touchpadEnabled === true ? "On"
    : settings.touchpadEnabled === false ? "Off" : "Unknown";
  const mode = goS ? "" : ` · ${VIBE_MODES[settings.mode ?? 0] ?? "Custom"}`;
  return `${VIBE_LEVELS[settings.level ?? 2] ?? "Custom"}${mode} · Touchpad ${touchpad}`;
};

export const displaySummary = (state?: DisplayState) => {
  const fix = !state?.edid_fix ? "HDR game fix off" : state.edid_patched ? "HDR game fix on" : "HDR game fix not active yet";
  return state?.steamos_update_needed ? `Update SteamOS to 3.9 · ${fix}` : fix;
};
