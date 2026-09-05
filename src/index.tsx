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
import { TdpPage, startTdpWatcher, stopTdpWatcher } from "./tdp";
import {
  VibrationPage,
  startVibrationWatcher,
  stopVibrationWatcher,
} from "./vibration";
import { getWifiStatus, WifiPage, wifiSummary, type WifiStatus } from "./wifi";
import { getRgbStatus, RgbPage, rgbSummary, type RgbStatus } from "./rgb";
import { getRemapStatus, RemapPage, remapSummary, type RemapStatus } from "./remap";
import { BatteryPage, getBatteryStatus, batterySummary, type BatteryStatus } from "./battery";
import { ControllerPage, getControllerStatus, controllerSummary, type ControllerStatus } from "./controller";

type ModuleKey = "tdp" | "vibration" | "display" | "wifi" | "rgb" | "remap" | "battery" | "controller";
type SectionKey = ModuleKey | "about" | "modules";
type ModuleStates = Partial<Record<ModuleKey, {enabled: boolean; pending?: boolean; error?: string; note?: string}>>;
const EMPTY_MODULES: ModuleStates = {};
const moduleLabels: Record<ModuleKey, string> = {tdp: "TDP & CPU", vibration: "Vibration", display: "OLED Display", wifi: "WiFi", rgb: "RGB Lighting", remap: "Button Remapper", battery: "Battery", controller: "Gyro & Touchpad"};
const moduleEnabled = (modules: ModuleStates, key: ModuleKey) => modules[key]?.enabled !== false && !modules[key]?.pending;
const setModuleEnabled = callable<[ModuleKey, boolean], ModuleStates>("modules_set_enabled");
const restartModulesSession = callable<[], {success: boolean; message?: string; error?: string}>("modules_restart_session");

interface TdpSettings {
  active_spl?: number;
  active_sppt?: number;
  active_fppt?: number;
  spl?: number;
  sppt?: number;
  fppt?: number;
}

interface VibeSettingsResponse {
  settings?: { level?: number; mode?: number };
}

interface DriverStatus { found?: boolean }

interface DisplayState {
  panel_mode?: "gamma22" | "pq" | "hybrid" | null;
  setup_done?: boolean;
  active?: boolean;
  edid_patched?: boolean;
  settings_error?: string;
  setup_error?: string;
}

interface Overview {
  version: string;
  standalonePlugins: string[];
  reads?: Partial<Record<OverviewField, { unavailable: boolean; note?: string }>>;
  tdp?: TdpSettings;
  vibration?: VibeSettingsResponse;
  driver?: DriverStatus;
  display?: DisplayState;
  wifi?: WifiStatus;
  rgb?: RgbStatus;
  remap?: RemapStatus;
  battery?: BatteryStatus;
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
let guardRevision = 0;
const updateGuard = (status: GuardStatus) => {
  if (!frontendActive) return;
  guardRevision += 1;
  // A late successful response must never unlock a latched conflict in this load.
  if (guardStatus?.blocked && !status.blocked) return;
  if (typeof status.blocked !== "boolean") status = { ...status, blocked: true,
    guard_error: "The backend has not provided its compatibility status. Restart Decky and try again." };
  guardStatus = status;
  overviewPaused = status.blocked;
  if (status.blocked) clearOverview();
  configureOverviewModules(status.modules ?? EMPTY_MODULES);
  overviewCache = { ...overviewCache, version: status.version,
    standalonePlugins: status.standalone_plugins ?? [] };
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
    if (frontendActive && revision === guardRevision) updateGuard(next);
  }
  catch {
    if (!frontendActive || revision !== guardRevision) return;
    // A connection failure hides controls and stops reports, but is not a
    // conflict latch: the user can retry once Decky responds again.
    tdpWatcherEnabled = vibeWatcherEnabled = false;
    stopTdpWatcher(); stopVibrationWatcher();
    overviewPaused = true;
    clearOverview();
    guardListeners.forEach(listener => listener({version: "0.6.2", blocked: true,
      guard_error: "Could not verify installed plugins. Check the Decky connection and try again."}));
  } finally { if (guardRead === request) guardRead = null; }
};
const getTdpSettings = callable<[], TdpSettings>("get_settings");
const getVibeSettings = callable<[], VibeSettingsResponse>("vibe_get_settings");
const getDriverStatus = callable<[], DriverStatus>("vibe_get_driver_status");
const getDisplayState = callable<[], DisplayState>("display_get_state");

// Keep confirmed summaries and in-flight reads across QAM remounts. Each source
// publishes independently, so a slow hardware/network probe cannot hide ready data.
type OverviewField = Exclude<keyof Overview, "version" | "standalonePlugins" | "reads">;
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
  { key: "vibration", module: "vibration", read: async () => ({ vibration: await getVibeSettings() }) },
  { key: "driver", module: "vibration", read: async () => ({ driver: await getDriverStatus() }) },
  { key: "display", module: "display", read: async () => ({ display: await getDisplayState() }) },
  { key: "wifi", module: "wifi", read: async () => ({ wifi: await getWifiStatus() }) },
  { key: "rgb", module: "rgb", read: async () => ({ rgb: await getRgbStatus() }) },
  { key: "remap", module: "remap", read: async () => ({ remap: await getRemapStatus() }) },
  { key: "battery", module: "battery", read: async () => ({ battery: await getBatteryStatus() }) },
  { key: "controller", module: "controller", read: async () => ({ controller: await getControllerStatus() }) },
].map(source => ({ ...source, revision: 0, pending: null,
  lastReadAt: null, failures: 0, pendingSince: null } as OverviewSource));
let overviewCache: Overview = { version: "0.6.2", standalonePlugins: [] };
let overviewModules = EMPTY_MODULES;
let overviewPaused = true;
const overviewListeners = new Set<(overview: Overview) => void>();
const overviewPollers = new Set<(overview: Overview) => void>();
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
  overviewCache = { version: "0.6.2", standalonePlugins: [] };
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
    if (overviewPollers.size) { source.failures += 1; publishOverview(); }
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
  const sources = overviewSources.filter(source => source.module === module);
  if (module === "display" && (overview.display?.settings_error || overview.display?.setup_error))
    return overview.display.settings_error || overview.display.setup_error || description;
  // A returned error/recovery state is a fresh report, not a rejected RPC.
  // Never replace it with an earlier healthy cache or a generic retry message.
  for (const source of sources) {
    const value = overview[source.key] as { error?: string; reason?: string; message?: string;
      success?: boolean; supported?: boolean; available?: boolean; recovery_pending?: boolean; recovery_required?: boolean } | undefined;
    if (value?.error) return (module === "wifi" && value.message) || value.error;
    if (value?.recovery_pending || value?.recovery_required) return value.reason
      || ((module === "battery" || module === "controller") && description) || "An interrupted change needs recovery";
    if (value?.success === false || value?.supported === false || value?.available === false)
      return value.reason || value.message || (value.success !== false && description) || "Status unavailable";
  }
  if (module === "vibration" && overview.driver?.found === false) return description;
  return sources.some(source => overview.reads?.[source.key]?.unavailable) ? "Status unavailable" : description;
};
const overviewNote = (overview: Overview, module: ModuleKey) => overviewSources
  .filter(source => source.module === module).map(source => overview.reads?.[source.key]?.note).filter(Boolean).join(" · ") || undefined;

const PageShell: FC<{ children: ReactNode }> = ({ children }) => (
  <div style={{ width: "100%", maxWidth: "100%", minWidth: 0, overflowX: "hidden", boxSizing: "border-box" }}>
    {children}
  </div>
);

const Chevron: FC = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    style={{ display: "block", width: "1em", height: "1em", flexShrink: 0 }}>
    <path d="m9 18 6-6-6-6" />
  </svg>
);

const SectionLink: FC<{
  title: string;
  description: string;
  statusNote?: string;
  onClick: () => void;
}> = ({ title, description, statusNote, onClick }) => (
  <PanelSectionRow>
    <Field
      label={title}
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

const SectionHeader: FC<{ title: string; onBack: () => void }> = ({ title, onBack }) => (
  <PanelSection title={title}>
    <PanelSectionRow>
      <ButtonItem layout="below" onClick={onBack}>‹ All Controls</ButtonItem>
    </PanelSectionRow>
  </PanelSection>
);

const watts = (value?: number) => value == null ? "–" : String(Math.round(value / 1000));

const tdpSummary = (settings?: TdpSettings) => {
  if (!settings) return "Power limits, profiles and CPU controls";
  const spl = settings.active_spl ?? settings.spl;
  const sppt = settings.active_sppt ?? settings.sppt;
  const fppt = settings.active_fppt ?? settings.fppt;
  return `${watts(spl)} / ${watts(sppt)} / ${watts(fppt)} W`;
};

const VIBE_LEVELS = ["Off", "Low", "Medium", "High"];
const VIBE_MODES = ["FPS", "Racing", "Standard", "SPG", "RPG"];

const vibrationSummary = (overview: Overview) => {
  if (overview.driver && !overview.driver.found) return "Controller driver not detected";
  const settings = overview.vibration?.settings;
  if (!settings) return "Handles, touchpad and per-game profiles";
  return `${VIBE_LEVELS[settings.level ?? 2] ?? "Custom"} · ${VIBE_MODES[settings.mode ?? 0] ?? "Custom"}`;
};

const displaySummary = (state?: DisplayState) => {
  if (!state?.setup_done) return "Choose Hybrid, PQ or Gamma 2.2";
  const mode = state.panel_mode === "gamma22"
    ? "Gamma 2.2"
    : state.panel_mode === "pq" ? "PQ" : "Hybrid";
  const fixes = [state.active ? "brightness active" : "brightness standby"];
  if (state.edid_patched) fixes.push("EDID fixed");
  return `${mode} · ${fixes.join(" · ")}`;
};

const ModulesPage: FC<{ modules: ModuleStates }> = ({ modules }) => {
  const [busy, setBusy] = useState<ModuleKey | null>(null);
  const [error, setError] = useState("");
  const writing = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const change = async (name: ModuleKey, enabled: boolean) => {
    if (writing.current) return;
    writing.current = true; setBusy(name); setError("");
    try {
      const next = await setModuleEnabled(name, enabled);
      if (guardStatus) updateGuard({...guardStatus, modules: next});
    } catch (failure) {
      if (mounted.current) setError(failure instanceof Error ? failure.message : "The module change could not be confirmed.");
      void refreshGuard();
    } finally { writing.current = false; if (mounted.current) setBusy(null); }
  };
  return <PanelSection title="Manage Modules">
    <PanelSectionRow><Field label="Choose your modules" description="Disabling stops the module, withdraws its hardware controls and hides its page. Saved preferences return when you enable it again. OLED can require a Gaming Mode restart." /></PanelSectionRow>
    {(Object.keys(moduleLabels) as ModuleKey[]).map(name => <PanelSectionRow key={name}>
      <ToggleField label={moduleLabels[name]} checked={moduleEnabled(modules, name)} disabled={busy !== null || !!modules[name]?.pending}
        description={busy === name ? "Applying and verifying the change…" : modules[name]?.error || modules[name]?.note || (moduleEnabled(modules, name) ? "Enabled" : "Disabled — hidden from the main menu")}
        onChange={enabled => void change(name, enabled)} />
      {modules[name]?.pending && <ButtonItem layout="below" disabled={busy !== null} onClick={() => void change(name, false)}>Retry {moduleLabels[name]} Cleanup</ButtonItem>}
    </PanelSectionRow>)}
    {error && <PanelSectionRow><Field label="Could not confirm" description={error} /></PanelSectionRow>}
    {modules.display?.note && <PanelSectionRow><ButtonItem layout="below" disabled={busy !== null} onClick={() => {
      void restartModulesSession().then(result => { if (!result.success && mounted.current) setError(result.error || result.message || "Gaming Mode restart failed."); }).catch(() => { if (mounted.current) setError("Could not confirm the Gaming Mode restart."); });
    }}>Restart Gaming Mode (closes games)</ButtonItem></PanelSectionRow>}
  </PanelSection>;
};

const Controls: FC<{modules?: ModuleStates}> = ({modules = EMPTY_MODULES} = {}) => {
  const visible = useQuickAccessVisible();
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const [overview, setOverview] = useState<Overview>(overviewCache);
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
    if (activeSection && activeSection in moduleLabels && !moduleEnabled(modules, activeSection as ModuleKey)) setActiveSection(null);
  }, [modules, activeSection]);

  useEffect(() => {
    // Steam overlays can temporarily hide Quick Access. Visibility controls
    // polling only; section navigation belongs to the explicit links/back button.
    if (activeSection) { invalidateOverviewReads(); return; }
    if (!visible) return;
    if (!overviewPollers.size) overviewVisibleStarted = Date.now();
    overviewPollers.add(setOverview);
    setOverview(overviewCache);
    refreshOverview();
    const timer = setInterval(refreshOverview, 10000);
    return () => {
      clearInterval(timer); overviewPollers.delete(setOverview);
      if (!overviewPollers.size) { overviewVisibleSpent = overviewVisibleTime(); overviewVisibleStarted = null; }
    };
  }, [activeSection, enabledModules, visible]);

  const summary = (module: ModuleKey, description: string) => ({
    description: overviewDescription(overview, module, description), statusNote: overviewNote(overview, module),
  });

  if (!activeSection) return <PageShell>
    <PanelSection title="Hardware Controls">
      {moduleEnabled(modules, "tdp") && <SectionLink title="TDP" {...summary("tdp", tdpSummary(overview.tdp))} onClick={() => setActiveSection("tdp")} />}
      {moduleEnabled(modules, "vibration") && <SectionLink title="Vibration" {...summary("vibration", vibrationSummary(overview))} onClick={() => setActiveSection("vibration")} />}
      {moduleEnabled(modules, "rgb") && <SectionLink title="RGB Lighting" {...summary("rgb", rgbSummary(overview.rgb))} onClick={() => setActiveSection("rgb")} />}
      {moduleEnabled(modules, "remap") && <SectionLink title="Button Remapper" {...summary("remap", remapSummary(overview.remap))} onClick={() => setActiveSection("remap")} />}
      {moduleEnabled(modules, "controller") && <SectionLink title="Gyro & Touchpad" {...summary("controller", controllerSummary(overview.controller))} onClick={() => setActiveSection("controller")} />}
      {moduleEnabled(modules, "battery") && <SectionLink title="Battery" {...summary("battery", batterySummary(overview.battery))} onClick={() => setActiveSection("battery")} />}
      {moduleEnabled(modules, "display") && <SectionLink title="OLED Display" {...summary("display", displaySummary(overview.display))} onClick={() => setActiveSection("display")} />}
      {moduleEnabled(modules, "wifi") && <SectionLink title="WiFi" {...summary("wifi", wifiSummary(overview.wifi))} onClick={() => setActiveSection("wifi")} />}
    </PanelSection>
    <PanelSection title="Device">
      <PanelSectionRow>
        <Field label="Lenovo Legion Go 2" description="OLED · SteamOS · Z2 Extreme support" />
      </PanelSectionRow>
    </PanelSection>
    <PanelSection title="Plugin">
      <SectionLink title="Manage Modules" description="Enable, disable and hide individual hardware modules" onClick={() => setActiveSection("modules")} />
      <SectionLink
        title="About"
        description={`Legion Go 2 Companion · v${overview.version}`}
        onClick={() => setActiveSection("about")}
      />
    </PanelSection>
  </PageShell>;

  const title = activeSection === "tdp"
    ? "TDP"
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
        ? "Gyro & Touchpad"
      : activeSection === "battery"
        ? "Battery"
      : activeSection === "modules" ? "Manage Modules" : "About";

  return <PageShell>
    <SectionHeader title={title} onBack={() => setActiveSection(null)} />
    {activeSection === "modules" && <ModulesPage modules={modules} />}
    {activeSection === "tdp" && <TdpPage />}
    {activeSection === "vibration" && <VibrationPage />}
    {activeSection === "display" && <DisplayPage />}
    {activeSection === "wifi" && <WifiPage />}
    {activeSection === "rgb" && <RgbPage />}
    {activeSection === "remap" && <RemapPage />}
    {activeSection === "controller" && <ControllerPage visible={visible} />}
    {activeSection === "battery" && <BatteryPage />}
    {activeSection === "about" && <>
      <PanelSection title="Legion Go 2 Companion">
        <PanelSectionRow>
          <Field label={`Version ${overview.version}`} description="All-in-one hardware controls for Decky Loader." />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Included modules" description="LeGoTDP · LeGo Vibe Control · LeGo2 Brightness Fix · WiFi Optimizer Go 2 · RGB Lighting · Button Remapper · Gyro & Touchpad · Battery" />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Author" description="Rayek · BSD-3-Clause open-source plugin. Vibration portions also retain their MIT notice." />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Updates">
        <PanelSectionRow>
          <Field label="Development build" description="Source is available on GitHub. No public release has been published yet." />
        </PanelSectionRow>
      </PanelSection>
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
    <PanelSectionRow><Field label="Checking compatibility" description="Hardware controls will appear after the check completes." /></PanelSectionRow>
  </PanelSection></PageShell>;
  if (status.blocked) return <PageShell>
    <PanelSection title="Companion paused">
      <PanelSectionRow><Field label="All modules are paused"
        description="All hardware controls, including gyro diagnostics and battery protection, are unavailable while a standalone plugin is installed." /></PanelSectionRow>
      {!!status.standalone_plugins?.length && <PanelSectionRow>
        <Field label="Installed standalone plugins" description={status.standalone_plugins.join(", ")} />
      </PanelSectionRow>}
      <PanelSectionRow><Field label={status.restart_required ? "Ready for a restart" : "How to resume"}
        description={status.guard_error || (status.restart_required
          ? "The conflicting plugins are gone. Restart Decky or the console to start Companion safely."
          : "In Decky Settings, uninstall the plugins listed above and keep their saved settings. Then restart Decky or the console. Disabling a plugin is not enough; it must be uninstalled.")} /></PanelSectionRow>
      <PanelSectionRow><Field label="Your settings are kept" description="This safeguard does not erase Companion profiles or the standalone settings available for import." /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" onClick={() => void refreshGuard()}>Check again</ButtonItem></PanelSectionRow>
    </PanelSection>
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
