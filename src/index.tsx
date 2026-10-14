// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { addEventListener, removeEventListener, callable, definePlugin, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { FC, ReactNode, useEffect, useRef, useState } from "react";
import { DisplayPage } from "./display";
import { TdpPage, startTdpWatcher, stopTdpWatcher, tdpModeLabel } from "./tdp";
import { FanPage, getFanState, fanSummary, type FanState } from "./fan";
import {
  VibrationPage,
  getVibrationSettings,
  startVibrationWatcher,
  stopVibrationWatcher,
} from "./vibration";
import { getWifiStatus, WifiPage, wifiSummary, type WifiStatus } from "./wifi";
import { getRgbStatus, RgbPage, rgbSummary, type RgbStatus } from "./rgb";
import { getRemapStatus, RemapPage, remapSummary, type RemapStatus } from "./remap";
import { BatteryPage, getBatteryStatus, getControllerBatteryLevels, batterySummary,
  type BatteryStatus, type ControllerBatteryStatus } from "./battery";
import { ControllerPage, getControllerStatus, controllerSummary, type ControllerStatus } from "./controller";
import { UpdateSection, startUpdates, stopUpdates } from "./updates";
import { FirmwarePanel } from "./firmware";

type ModuleKey = "tdp" | "fan" | "vibration" | "display" | "wifi" | "rgb" | "remap" | "battery" | "controller";
type SectionKey = ModuleKey | "about" | "modules";
type ModuleStates = Partial<Record<ModuleKey, {enabled: boolean; ready?: boolean; pending?: boolean; maintenance?: boolean; error?: string; note?: string}>>;
const EMPTY_MODULES: ModuleStates = {};
const moduleLabels: Record<ModuleKey, string> = {tdp: "TDP & CPU", fan: "Fan Control", vibration: "Vibration", display: "OLED Display", wifi: "WiFi", rgb: "RGB Lighting", remap: "Button Remapper", battery: "Battery", controller: "Gyro & Touchpad"};
const moduleEnabled = (modules: ModuleStates, key: ModuleKey) => modules[key]?.enabled !== false && modules[key]?.ready !== false && !modules[key]?.pending;
const setModuleEnabled = callable<[ModuleKey, boolean], ModuleStates>("modules_set_enabled");
const restartModulesSession = callable<[], {success: boolean; message?: string; error?: string}>("modules_restart_session");

interface TdpSettings {
  enabled?: boolean;
  active_preset?: string;
  active_spl?: number;
  active_sppt?: number;
  active_fppt?: number;
  spl?: number;
  sppt?: number;
  fppt?: number;
}

interface VibeSettingsResponse {
  settings?: { level?: number; mode?: number; touchpadEnabled?: boolean };
}

interface DriverStatus { found?: boolean }

interface DisplayState {
  native_support_detected?: boolean;
  native_support_reason?: string;
  native_cleanup_pending?: boolean;
  native_cleanup_error?: string;
  native_restart_pending?: boolean;
  panel_mode?: "gamma22" | "pq" | "hybrid" | null;
  setup_done?: boolean;
  active?: boolean;
  edid_patched?: boolean;
  settings_error?: string;
  setup_error?: string;
}

interface Overview {
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

interface GuardStatus {
  modules?: ModuleStates;
  version: string;
  standalone_plugins?: string[];
  blocked: boolean;
  restart_required?: boolean;
  guard_error?: string;
  message?: string;
}
const getVersion = callable<[], GuardStatus>("get_version");
let guardStatus: GuardStatus | null = null;
const guardListeners = new Set<(status: GuardStatus) => void>();
let tdpWatcherEnabled = false;
let vibeWatcherEnabled = false;
let frontendActive = false;
let guardRead: Promise<GuardStatus> | null = null;
let guardUnavailable = false;
let guardRevision = 0;
const validGuard = (status: GuardStatus) => !!status && typeof status === "object" && !Array.isArray(status) && typeof status.blocked === "boolean";
const pauseUnavailableGuard = () => {
  if (!guardUnavailable) console.warn("[legiongo2companion] frontend.connection_unavailable");
  guardUnavailable = true;
  // A missing/malformed reply is transient, not a latched plugin conflict.
  tdpWatcherEnabled = vibeWatcherEnabled = false;
  stopTdpWatcher(); stopVibrationWatcher();
  overviewPaused = true;
  clearOverview();
  guardListeners.forEach(listener => listener({version: "2.1.0", blocked: true,
    guard_error: "Could not verify installed plugins. Check the Decky connection and try again."}));
};
const updateGuard = (status: GuardStatus) => {
  if (!frontendActive) return;
  guardRevision += 1;
  if (!validGuard(status)) { pauseUnavailableGuard(); return; }
  // A late successful response must never unlock a latched conflict in this load.
  if (guardStatus?.blocked && !status.blocked) return;
  guardStatus = status;
  overviewPaused = status.blocked;
  if (status.blocked) clearOverview();
  configureOverviewModules(status.modules ?? EMPTY_MODULES);
  overviewCache = { ...overviewCache, version: status.version };
  publishOverview();
  const enabled = status.blocked === false;
  const tdpEnabled = enabled && moduleEnabled(status.modules ?? {}, "tdp");
  const vibeEnabled = enabled && moduleEnabled(status.modules ?? {}, "vibration");
  if (tdpEnabled !== tdpWatcherEnabled) { tdpWatcherEnabled = tdpEnabled; tdpEnabled ? startTdpWatcher() : stopTdpWatcher(); }
  if (vibeEnabled !== vibeWatcherEnabled) { vibeWatcherEnabled = vibeEnabled; vibeEnabled ? startVibrationWatcher() : stopVibrationWatcher(); }
  guardListeners.forEach(listener => listener(status));
};
const refreshGuard = async () => {
  if (!frontendActive || guardRead) return;
  const revision = guardRevision;
  const request = getVersion();
  guardRead = request;
  try {
    const next = await request;
    if (frontendActive && revision === guardRevision) {
      if (!validGuard(next)) throw new Error("Compatibility status is unavailable.");
      if (guardUnavailable) console.info("[legiongo2companion] frontend.connection_restored");
      guardUnavailable = false;
      updateGuard(next);
    }
  }
  catch {
    if (!frontendActive || revision !== guardRevision) return;
    pauseUnavailableGuard();
  } finally { if (guardRead === request) guardRead = null; }
};
const getTdpSettings = callable<[], TdpSettings>("get_settings");
const getVibeSettings = getVibrationSettings;
const getDriverStatus = callable<[], DriverStatus>("vibe_get_driver_status");
const getDisplayState = callable<[], DisplayState>("display_get_state");

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
const overviewSources: OverviewSource[] = [
  { key: "tdp", module: "tdp", read: async () => ({ tdp: await getTdpSettings() }) },
  { key: "fan", module: "fan", read: async () => ({ fan: await getFanState(() =>
    frontendActive && !overviewPaused && !!overviewPollers.size && [...overviewViewChecks].some(check => check())) }) },
  { key: "vibration", module: "vibration", read: async () => ({ vibration: await getVibeSettings() }) },
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
let overviewCache: Overview = { version: "2.1.0" };
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
const invalidateOverviewReads = () => {
  overviewSources.forEach(source => { source.revision += 1; source.failures = 0; source.pendingSince = null; });
  overviewCache = { ...overviewCache, reads: {} };
};
const clearOverview = () => {
  invalidateOverviewReads();
  overviewSources.forEach(source => { source.lastReadAt = null; });
  overviewCache = { version: "2.1.0" };
};
const configureOverviewModules = (modules: ModuleStates) => {
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
  if (!frontendActive || overviewPaused || !overviewPollers.size ||
      !moduleEnabled(overviewModules, source.module)) return;
  if (source.pendingSince === null) source.pendingSince = overviewVisibleTime();
  if (source.pending) return;
  const revision = source.revision;
  const request = source.read();
  source.pending = request;
  void request.then(value => {
    if (!frontendActive || overviewPaused || revision !== source.revision) return;
    if (!value[source.key] || typeof value[source.key] !== "object" || Array.isArray(value[source.key]))
      throw new Error("Status unavailable");
    source.lastReadAt = Date.now(); source.failures = 0; source.pendingSince = null;
    overviewCache = { ...overviewCache, ...value };
    publishOverview();
  }).catch(() => {
    if (!frontendActive || overviewPaused || revision !== source.revision) return;
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
  if (!frontendActive || overviewPaused || !overviewPollers.size) return;
  overviewSources.forEach(readOverviewSource);
  publishOverview();
};

const overviewDescription = (overview: Overview, module: ModuleKey, description: string) => {
  if (module === "fan") return description; // Keep measured numbers separate from fault status.
  const sources = overviewSources.filter(source => source.module === module);
  if (module === "display") {
    const display = overview.display;
    const native = display?.native_support_detected || display?.native_cleanup_pending;
    const error = display?.settings_error || (native ? display?.native_cleanup_error : display?.setup_error);
    if (error) return error;
  }
  // A returned error/recovery state is a fresh report, not a rejected RPC.
  // Never replace it with an earlier healthy cache or a generic retry message.
  for (const source of sources) {
    if (source.key === "controller_batteries") continue;
    const value = overview[source.key] as { error?: string; reason?: string; message?: string;
      success?: boolean; supported?: boolean; available?: boolean; recovery_pending?: boolean; recovery_required?: boolean } | undefined;
    if (value?.error) return (module === "wifi" && value.message) || value.error;
    if (value?.recovery_pending || value?.recovery_required) return value.reason
      || ((module === "battery" || module === "controller") && description) || "An interrupted change needs recovery";
    if (value?.success === false || value?.supported === false || value?.available === false)
      return value.reason || value.message || (value.success !== false && description) || "Status unavailable";
  }
  if (module === "vibration" && overview.driver?.found === false) return description;
  return sources.some(source => source.key !== "controller_batteries"
    && overview.reads?.[source.key]?.unavailable) ? "Status unavailable" : description;
};
const overviewNote = (overview: Overview, module: ModuleKey) => overviewSources
  .filter(source => source.module === module).map(source => overview.reads?.[source.key]?.note).filter(Boolean).join(" · ") || undefined;

const PageShell: FC<{ children: ReactNode; rootRef?: {current: HTMLDivElement | null} }> = ({ children, rootRef }) => (
  <div ref={rootRef} data-companion-page style={{ width: "100%", maxWidth: "100%", minWidth: 0, overflowX: "hidden", boxSizing: "border-box" }}>
    {children}
  </div>
);

const Chevron: FC = () => (
  <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    style={{ display: "block", width: "1em", height: "1em", flexShrink: 0 }}>
    <path d="m9 18 6-6-6-6" />
  </svg>
);

// Field supplies Steam's label, spacing, focus highlight and activation. The
// decorative icons inherit that same color, including its focused appearance.
const sectionIconPaths: Record<SectionKey, ReactNode> = {
  tdp: <path d="m13 2-9 12h7l-1 8 10-12h-7l1-8Z" />,
  fan: <><circle cx="12" cy="12" r="2" /><path d="M12 10C4 1 1 12 10 12M14 12c9-8-2-11-2-2M12 14c8 9 11-2 2-2M10 12c-9 8 2 11 2 2" /></>,
  vibration: <><rect x="7" y="5" width="10" height="14" rx="2" /><path d="m3 7-1 3 1 4-1 3m19-10 1 3-1 4 1 3" /></>,
  rgb: <><path d="M12 3a9 9 0 1 0 0 18h1a2 2 0 0 0 1-3.7 2 2 0 0 1 1-3.3h2a4 4 0 0 0 4-4c0-4-4-7-9-7Z" /><circle cx="7" cy="10" r=".8" /><circle cx="10" cy="7" r=".8" /><circle cx="15" cy="7" r=".8" /></>,
  remap: <><path d="M7 7h10a4 4 0 0 1 4 4l-1 7-5-3H9l-5 3-1-7a4 4 0 0 1 4-4Z" /><path d="M6 11h4m-2-2v4m7-2h.01m3 2h.01" /></>,
  controller: <><path d="M12 3v18M3 12h18m-9-9-3 3m3-3 3 3m6 6-3-3m3 3-3 3m-6 6-3-3m3 3 3-3M3 12l3-3m-3 3 3 3" /></>,
  battery: <><rect x="3" y="6" width="17" height="12" rx="2" /><path d="M22 10v4m-9-6-4 5h5l-3 4" /></>,
  display: <><rect x="3" y="4" width="18" height="13" rx="2" /><path d="M8 21h8m-4-4v4" /></>,
  wifi: <><path d="M2 8a16 16 0 0 1 20 0M5 12a11 11 0 0 1 14 0m-11 4a6 6 0 0 1 8 0" /><circle cx="12" cy="20" r=".8" /></>,
  modules: <><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>,
  about: <><circle cx="12" cy="12" r="9" /><path d="M12 11v6m0-10h.01" /></>,
};
const SectionIcon: FC<{ section: SectionKey }> = ({ section }) => (
  <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"
    style={{ display: "block", width: "1.2em", height: "1.2em", flexShrink: 0 }}>
    {sectionIconPaths[section]}
  </svg>
);

const SectionLink: FC<{
  section: SectionKey;
  title: string;
  description: string;
  statusNote?: string;
  restoreFocus?: boolean;
  onClick: () => void;
}> = ({ section, title, description, statusNote, restoreFocus, onClick }) => {
  const fieldRef = useRef<HTMLDivElement>(null);
  const visible = useQuickAccessVisible();
  useEffect(() => {
    if (restoreFocus && visible) fieldRef.current?.focus();
    // Returning to the list restores its selection once. QAM/dropdown visibility
    // and status polling must not move the user's current Steam focus.
  }, [restoreFocus]);
  return (
  <PanelSectionRow>
    <Field
      ref={fieldRef}
      label={title}
      icon={<SectionIcon section={section} />}
      bottomSeparator="standard"
      verticalAlignment="center"
      description={statusNote ? <>{description}<div style={{ fontSize: "11px", opacity: 0.7, marginTop: "2px" }}>{statusNote}</div></> : description}
      childrenLayout="inline"
      childrenContainerWidth="min"
      focusable
      highlightOnFocus
      onActivate={onClick}
    >
      <Chevron />
    </Field>
  </PanelSectionRow>
  );
};

const SectionHeader: FC<{ title: string; onBack: () => void }> = ({ title, onBack }) => {
  const backRef = useRef<HTMLDivElement>(null);
  const visible = useQuickAccessVisible();
  useEffect(() => {
    if (visible) backRef.current?.querySelector<HTMLButtonElement>("button")?.focus();
    // One focus handoff on page entry, never on a poll or dropdown dismissal.
  }, []);
  return <PanelSection title={title}>
    <PanelSectionRow>
      <div ref={backRef}><ButtonItem layout="below" onClick={onBack}>‹ All Controls</ButtonItem></div>
    </PanelSectionRow>
  </PanelSection>;
};

const watts = (value?: number) => value == null ? "–" : String(Math.round(value / 1000));

const tdpSummary = (settings?: TdpSettings) => {
  if (!settings) return "Power limits, profiles and CPU controls";
  const spl = settings.enabled === false ? settings.spl : settings.active_spl ?? settings.spl;
  const sppt = settings.enabled === false ? settings.sppt : settings.active_sppt ?? settings.sppt;
  const fppt = settings.enabled === false ? settings.fppt : settings.active_fppt ?? settings.fppt;
  const global = spl === settings.spl && sppt === settings.sppt && fppt === settings.fppt;
  const mode = settings.enabled === false ? "Off" : spl != null && sppt != null && fppt != null
    ? tdpModeLabel(Math.round(spl / 1000), Math.round(sppt / 1000), Math.round(fppt / 1000),
      global ? settings.active_preset : undefined) : "Custom";
  return `${mode} · ${watts(spl)} / ${watts(sppt)} / ${watts(fppt)} W`;
};

const VIBE_LEVELS = ["Off", "Low", "Medium", "High"];
const VIBE_MODES = ["FPS", "Racing", "Standard", "SPG", "RPG"];

const vibrationSummary = (overview: Overview) => {
  if (overview.driver && !overview.driver.found) return "Controller driver not detected";
  const settings = overview.vibration?.settings;
  if (!settings) return "Handles, touchpad and per-game profiles";
  const touchpad = settings.touchpadEnabled === true ? "On"
    : settings.touchpadEnabled === false ? "Off" : "Unknown";
  return `${VIBE_LEVELS[settings.level ?? 2] ?? "Custom"} · ${VIBE_MODES[settings.mode ?? 0] ?? "Custom"} · Touchpad ${touchpad}`;
};

const displaySummary = (state?: DisplayState) => {
  if (state?.native_support_detected || state?.native_cleanup_pending) {
    const status = state.native_cleanup_error ? "Display fix cleanup needs attention"
      : state.native_cleanup_pending ? "Display fix cleanup pending"
      : state.native_restart_pending ? "Restart required"
      : "Native display support";
    return `${status} · ${state.edid_patched ? "EDID fixed" : "EDID standby"}`;
  }
  if (!state?.setup_done) return "Choose Hybrid, PQ or Gamma 2.2";
  const mode = state.panel_mode === "gamma22"
    ? "Gamma 2.2"
    : state.panel_mode === "pq" ? "PQ" : "Hybrid";
  const fixes = [state.active ? "brightness active" : "brightness standby"];
  if (state.edid_patched) fixes.push("EDID fixed");
  return `${mode} · ${fixes.join(" · ")}`;
};

const ModulesPage: FC<{ modules: ModuleStates }> = ({ modules }) => {
  const [busy, setBusy] = useState<ModuleKey | "restart" | null>(null);
  const [error, setError] = useState("");
  const writing = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const change = async (name: ModuleKey, enabled: boolean) => {
    if (!mounted.current || writing.current) return;
    writing.current = true; setBusy(name); setError("");
    try {
      const next = await setModuleEnabled(name, enabled);
      if (guardStatus) updateGuard({...guardStatus, modules: next});
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : "The module change could not be confirmed.");
      void refreshGuard();
    } finally { writing.current = false; if (mounted.current) setBusy(null); }
  };
  const restart = async () => {
    if (!mounted.current || writing.current) return;
    writing.current = true; setBusy("restart"); setError("");
    try {
      const result = await restartModulesSession();
      if (!result.success && mounted.current) setError(result.error || result.message || "Gaming Mode restart failed.");
    } catch {
      if (mounted.current) setError("Could not confirm the Gaming Mode restart.");
    } finally { writing.current = false; if (mounted.current) setBusy(null); }
  };
  return <PanelSection title="Manage Modules">
    <PanelSectionRow><Field focusable label="Choose your modules" description="Disabling stops the module, withdraws its hardware controls and hides its page. Saved preferences return when you enable it again. OLED can require a Gaming Mode restart." /></PanelSectionRow>
    {(Object.keys(moduleLabels) as ModuleKey[]).map(name => <PanelSectionRow key={name}>
      <ToggleField label={moduleLabels[name]} checked={modules[name]?.maintenance ? modules[name]?.enabled === true : moduleEnabled(modules, name)} disabled={busy !== null || !!modules[name]?.pending || !!modules[name]?.maintenance}
        description={busy === name ? "Applying and verifying the change…" : modules[name]?.error || modules[name]?.note || (moduleEnabled(modules, name) ? "Enabled" : "Disabled — hidden from the main menu")}
        onChange={enabled => void change(name, enabled)} />
      {modules[name]?.pending && !modules[name]?.maintenance && <ButtonItem layout="below" disabled={busy !== null} onClick={() => void change(name, false)}>Retry {moduleLabels[name]} Cleanup</ButtonItem>}
      {modules[name]?.enabled && modules[name]?.ready === false && !modules[name]?.pending && !modules[name]?.maintenance &&
        <ButtonItem layout="below" disabled={busy !== null} onClick={() => void change(name, true)}>Retry {moduleLabels[name]} Start</ButtonItem>}
    </PanelSectionRow>)}
    {error && <PanelSectionRow><Field focusable label="Could not confirm" description={error} /></PanelSectionRow>}
    {modules.display?.note && <PanelSectionRow><ButtonItem layout="below" disabled={busy !== null} onClick={() => void restart()}>Restart Gaming Mode (closes games)</ButtonItem></PanelSectionRow>}
  </PanelSection>;
};

const Controls: FC<{modules?: ModuleStates}> = ({modules = EMPTY_MODULES} = {}) => {
  const visible = useQuickAccessVisible();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [onScreen, setOnScreen] = useState(true);
  const panelOnScreen = () => !panelRef.current || panelRef.current.getClientRects().length > 0;
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const [overview, setOverview] = useState<Overview>(overviewCache);
  useEffect(() => {
    const element = panelRef.current;
    if (!element) return;
    setOnScreen(panelOnScreen());
    const Observer = (element.ownerDocument.defaultView as (Window & typeof globalThis) | null)?.IntersectionObserver;
    if (!Observer) return;
    // Steam can hide the Decky tab with display:none while QAM remains open.
    const observer = new Observer(entries => setOnScreen(entries.some(entry => entry.isIntersecting)));
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const lastOpenedSection = useRef<SectionKey | null>(null);
  const openSection = (section: SectionKey) => {
    lastOpenedSection.current = section;
    setActiveSection(section);
  };
  const focusSection = lastOpenedSection.current && lastOpenedSection.current in moduleLabels
    && lastOpenedSection.current !== "controller" && !moduleEnabled(modules, lastOpenedSection.current as ModuleKey)
    ? "modules" : lastOpenedSection.current;
  useEffect(() => {
    // An already-running read may finish while QAM is hidden. Keep the mounted
    // view in sync with its cache without starting further hardware reads.
    overviewListeners.add(setOverview);
    return () => { overviewListeners.delete(setOverview); };
  }, []);
  // get_version supplies a fresh modules object on every guard check. Depend
  // only on effective enablement, not object identity, to preserve pending reads.
  const enabledModules = (Object.keys(moduleLabels) as ModuleKey[])
    .map(key => moduleEnabled(modules, key) ? "1" : "0").join("");
  useEffect(() => {
    configureOverviewModules(modules);
    setOverview(overviewCache);
  }, [enabledModules]);
  useEffect(() => {
    if (activeSection && activeSection !== "controller" && activeSection in moduleLabels && !moduleEnabled(modules, activeSection as ModuleKey)) setActiveSection(null);
  }, [modules, activeSection]);

  useEffect(() => {
    // Steam overlays can temporarily hide Quick Access. Visibility controls
    // polling only; section navigation belongs to the explicit links/back button.
    if (activeSection) { invalidateOverviewReads(); return; }
    if (!visible || !onScreen) return;
    if (!overviewPollers.size) overviewVisibleStarted = Date.now();
    overviewPollers.add(setOverview);
    overviewViewChecks.add(panelOnScreen);
    setOverview(overviewCache);
    refreshOverview();
    const timer = setInterval(refreshOverview, 10000);
    const fanSource = overviewSources.find(source => source.key === "fan")!;
    const fanTimer = moduleEnabled(modules, "fan") ? setInterval(() => {
      readOverviewSource(fanSource); publishOverview();
    }, 1000) : undefined;
    return () => {
      clearInterval(timer); if (fanTimer !== undefined) clearInterval(fanTimer); overviewPollers.delete(setOverview);
      overviewViewChecks.delete(panelOnScreen);
      if (!overviewPollers.size) { overviewVisibleSpent = overviewVisibleTime(); overviewVisibleStarted = null; }
    };
  }, [activeSection, enabledModules, visible, onScreen]);

  const summary = (module: ModuleKey, description: string) => ({
    description: overviewDescription(overview, module, description), statusNote: overviewNote(overview, module),
  });

  if (!activeSection) return <PageShell rootRef={panelRef}>
    <PanelSection title="Hardware Controls">
      {Object.values(modules).some(state => state?.maintenance) && <PanelSectionRow><Field focusable label="Controller controls paused" description="Firmware maintenance is in progress. Saved controller settings have not been changed." /></PanelSectionRow>}
      {moduleEnabled(modules, "tdp") && <SectionLink section="tdp" restoreFocus={focusSection === "tdp"} title="TDP" {...summary("tdp", tdpSummary(overview.tdp))} onClick={() => openSection("tdp")} />}
      {moduleEnabled(modules, "fan") && <SectionLink section="fan" restoreFocus={focusSection === "fan"} title="Fan Control" {...summary("fan", fanSummary(overview.fan))} onClick={() => openSection("fan")} />}
      {moduleEnabled(modules, "vibration") && <SectionLink section="vibration" restoreFocus={focusSection === "vibration"} title="Vibration" {...summary("vibration", vibrationSummary(overview))} onClick={() => openSection("vibration")} />}
      {moduleEnabled(modules, "rgb") && <SectionLink section="rgb" restoreFocus={focusSection === "rgb"} title="RGB Lighting" {...summary("rgb", rgbSummary(overview.rgb))} onClick={() => openSection("rgb")} />}
      {moduleEnabled(modules, "remap") && <SectionLink section="remap" restoreFocus={focusSection === "remap"} title="Button Remapper" {...summary("remap", remapSummary(overview.remap))} onClick={() => openSection("remap")} />}
      <SectionLink section="controller" restoreFocus={focusSection === "controller"}
        title={moduleEnabled(modules, "controller") ? "Gyro & Touchpad" : "Controller firmware"}
        {...(moduleEnabled(modules, "controller") ? summary("controller", controllerSummary(overview.controller))
          : { description: "Controller update status and Lenovo firmware updates" })}
        onClick={() => openSection("controller")} />
      {moduleEnabled(modules, "battery") && <SectionLink section="battery" restoreFocus={focusSection === "battery"} title="Battery" {...summary("battery", batterySummary(overview.battery, overview.controller_batteries))} onClick={() => openSection("battery")} />}
      {moduleEnabled(modules, "display") && <SectionLink section="display" restoreFocus={focusSection === "display"} title="OLED Display" {...summary("display", displaySummary(overview.display))} onClick={() => openSection("display")} />}
      {moduleEnabled(modules, "wifi") && <SectionLink section="wifi" restoreFocus={focusSection === "wifi"} title="WiFi" {...summary("wifi", wifiSummary(overview.wifi))} onClick={() => openSection("wifi")} />}
    </PanelSection>
    <PanelSection title="Device">
      <PanelSectionRow>
        <Field focusable label="Lenovo Legion Go 2" description="OLED · SteamOS · Z2 Extreme support" />
      </PanelSectionRow>
    </PanelSection>
    <PanelSection title="Plugin">
      <SectionLink section="modules" restoreFocus={focusSection === "modules"} title="Manage Modules" description="Enable, disable and hide individual hardware modules" onClick={() => openSection("modules")} />
      <SectionLink
        section="about" restoreFocus={focusSection === "about"}
        title="About"
        description={`Legion Go 2 Companion · v${overview.version}`}
        onClick={() => openSection("about")}
      />
    </PanelSection>
  </PageShell>;

  const title = activeSection === "tdp"
    ? "TDP"
    : activeSection === "fan" ? "Fan Control"
    : activeSection === "vibration"
      ? "Vibration"
      : activeSection === "display"
        ? "OLED Display"
      : activeSection === "wifi"
        ? "WiFi"
      : activeSection === "rgb"
        ? "RGB Lighting"
      : activeSection === "remap"
        ? "Button Remapper"
      : activeSection === "controller"
        ? moduleEnabled(modules, "controller") ? "Gyro & Touchpad" : "Controller firmware"
      : activeSection === "battery"
        ? "Battery"
      : activeSection === "modules" ? "Manage Modules" : "About";

  return <PageShell rootRef={panelRef}>
    <SectionHeader title={title} onBack={() => setActiveSection(null)} />
    {activeSection === "modules" && <ModulesPage modules={modules} />}
    {activeSection === "tdp" && <TdpPage />}
    {activeSection === "fan" && <FanPage viewVisible={onScreen} isViewVisible={panelOnScreen} />}
    {activeSection === "vibration" && <VibrationPage />}
    {activeSection === "display" && <DisplayPage />}
    {activeSection === "wifi" && <WifiPage />}
    {activeSection === "rgb" && <RgbPage />}
    {activeSection === "remap" && <RemapPage />}
    {activeSection === "controller" && (moduleEnabled(modules, "controller") ? <ControllerPage visible={visible} />
      : <FirmwarePanel visible={visible} allowUpdates={!Object.values(modules).some(state => state?.maintenance)} />)}
    {activeSection === "battery" && <BatteryPage />}
    {activeSection === "about" && <>
      <PanelSection title="Legion Go 2 Companion">
        <PanelSectionRow>
          <Field focusable label={`Version ${overview.version}`} description="All-in-one hardware controls for Decky Loader." />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field focusable label="Included modules" description="LeGoTDP · Fan Control · LeGo Vibe Control · LeGo2 Brightness Fix · WiFi Optimizer Go 2 · RGB Lighting · Button Remapper · Gyro & Touchpad · Battery" />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field focusable label="Author" description="Rayek · BSD-3-Clause open-source plugin. Vibration portions also retain their MIT notice." />
        </PanelSectionRow>
      </PanelSection>
      <UpdateSection currentVersion={overview.version} />
    </>}
  </PageShell>;
};

const Content: FC = () => {
  const visible = useQuickAccessVisible();
  const [status, setStatus] = useState<GuardStatus | null>(guardStatus);
  useEffect(() => {
    guardListeners.add(setStatus);
    return () => { guardListeners.delete(setStatus); };
  }, []);
  useEffect(() => {
    if (!visible) return;
    void refreshGuard();
    const timer = setInterval(() => void refreshGuard(), 10000);
    return () => clearInterval(timer);
  }, [visible]);

  if (!status) return <PageShell><PanelSection title="Checking installed plugins">
    <PanelSectionRow><Field focusable label="Checking compatibility" description="Hardware controls will appear after the check completes." /></PanelSectionRow>
  </PanelSection></PageShell>;
  if (status.blocked) return <PageShell>
    <PanelSection title="Companion paused">
      <PanelSectionRow><Field focusable label="All modules are paused"
        description="All hardware controls, including gyro diagnostics and battery protection, are unavailable while a standalone plugin is installed." /></PanelSectionRow>
      {!!status.standalone_plugins?.length && <PanelSectionRow>
        <Field focusable label="Installed standalone plugins" description={status.standalone_plugins.join(", ")} />
      </PanelSectionRow>}
      <PanelSectionRow><Field focusable label={status.restart_required ? "Ready for a restart" : "How to resume"}
        description={status.guard_error || (status.restart_required
          ? "The conflicting plugins are gone. Restart Decky or the console to start Companion safely."
          : "In Decky Settings, uninstall the plugins listed above and keep their saved settings. Then restart Decky or the console. Disabling a plugin is not enough; it must be uninstalled.")} /></PanelSectionRow>
      <PanelSectionRow><Field focusable label="Your settings are kept" description="This safeguard does not erase Companion profiles or the standalone settings available for import." /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" onClick={() => void refreshGuard()}>Check again</ButtonItem></PanelSectionRow>
    </PanelSection>
    <FirmwarePanel visible={visible} allowUpdates={false} />
  </PageShell>;
  return <Controls modules={status.modules} />;
};

const Icon: FC = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    style={{ width: "1em", height: "1em" }}>
    <path d="M7 8h10a5 5 0 0 1 4.7 6.7l-1.1 3.1a2 2 0 0 1-3.3.8L15 16H9l-2.3 2.6a2 2 0 0 1-3.3-.8l-1.1-3.1A5 5 0 0 1 7 8Z" />
    <circle cx="8" cy="12" r="2" />
    <circle cx="16" cy="12" r="2" />
  </svg>
);

export default definePlugin(() => {
  startUpdates();
  frontendActive = true;
  guardRevision += 1;
  guardRead = null;
  guardStatus = null;
  clearOverview();
  overviewPaused = true;
  overviewModules = EMPTY_MODULES;
  overviewListeners.clear();
  overviewPollers.clear();
  overviewVisibleStarted = null; overviewVisibleSpent = 0;
  overviewSources.forEach(source => { source.pending = null; });
  const listener = addEventListener<[GuardStatus]>("companion_guard", updateGuard);
  void refreshGuard();
  return {
    name: "Legion Go 2 Companion",
    titleView: <div className={staticClasses.Title}>Legion Go 2 Companion</div>,
    // Native dropdowns hide QAM while their menu is open. Keep the component
    // tree (including navigation/focus) mounted; page polling is visibility-gated.
    alwaysRender: true,
    content: <Content />,
    icon: <Icon />,
    onDismount() {
      stopUpdates();
      frontendActive = false;
      guardRevision += 1;
      guardRead = null;
      clearOverview();
      overviewPaused = true;
      overviewListeners.clear();
      overviewPollers.clear();
      overviewVisibleStarted = null; overviewVisibleSpent = 0;
      tdpWatcherEnabled = vibeWatcherEnabled = false;
      removeEventListener("companion_guard", listener);
      guardListeners.clear();
      guardStatus = null;
      stopTdpWatcher();
      stopVibrationWatcher();
    },
  };
});
