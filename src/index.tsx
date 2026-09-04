// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { addEventListener, removeEventListener, callable, definePlugin, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  staticClasses,
} from "@decky/ui";
import { FC, ReactNode, useCallback, useEffect, useState } from "react";
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

type SectionKey = "tdp" | "vibration" | "display" | "wifi" | "rgb" | "remap" | "about";

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
}

interface Overview {
  version: string;
  standalonePlugins: string[];
  tdp?: TdpSettings;
  vibration?: VibeSettingsResponse;
  driver?: DriverStatus;
  display?: DisplayState;
  wifi?: WifiStatus;
  rgb?: RgbStatus;
  remap?: RemapStatus;
}

interface GuardStatus {
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
let watchersEnabled = false;
let frontendActive = false;
const updateGuard = (status: GuardStatus) => {
  if (!frontendActive) return;
  // A late successful response must never unlock a latched conflict in this load.
  if (guardStatus?.blocked && !status.blocked) return;
  if (typeof status.blocked !== "boolean") status = { ...status, blocked: true,
    guard_error: "The backend has not provided its compatibility status. Restart Decky and try again." };
  guardStatus = status;
  const enabled = status.blocked === false;
  if (enabled !== watchersEnabled) {
    watchersEnabled = enabled;
    if (enabled) { startTdpWatcher(); startVibrationWatcher(); }
    else { stopTdpWatcher(); stopVibrationWatcher(); }
  }
  guardListeners.forEach(listener => listener(status));
};
const refreshGuard = async () => {
  try { updateGuard(await getVersion()); }
  catch {
    // A connection failure hides controls and stops reports, but is not a
    // conflict latch: the user can retry once Decky responds again.
    watchersEnabled = false;
    stopTdpWatcher(); stopVibrationWatcher();
    guardListeners.forEach(listener => listener({version: "0.4.5", blocked: true,
      guard_error: "Could not verify installed plugins. Check the Decky connection and try again."}));
  }
};
const getTdpSettings = callable<[], TdpSettings>("get_settings");
const getVibeSettings = callable<[], VibeSettingsResponse>("vibe_get_settings");
const getDriverStatus = callable<[], DriverStatus>("vibe_get_driver_status");
const getDisplayState = callable<[], DisplayState>("display_get_state");

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
  onClick: () => void;
}> = ({ title, description, onClick }) => (
  <PanelSectionRow>
    <Field
      label={title}
      description={description}
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

const Controls: FC = () => {
  const visible = useQuickAccessVisible();
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const [overview, setOverview] = useState<Overview>({ version: "0.4.5", standalonePlugins: [] });

  const refresh = useCallback(async () => {
    const [version, tdp, vibration, driver, display, wifi, rgb, remap] = await Promise.all([
      getVersion().catch(() => ({ version: "0.4.5", standalone_plugins: [] })),
      getTdpSettings().catch(() => undefined),
      getVibeSettings().catch(() => undefined),
      getDriverStatus().catch(() => undefined),
      getDisplayState().catch(() => undefined),
      getWifiStatus().catch(() => undefined),
      getRgbStatus().catch(() => undefined),
      getRemapStatus().catch(() => undefined),
    ]);
    setOverview({
      version: version.version,
      standalonePlugins: version.standalone_plugins ?? [],
      tdp,
      vibration,
      driver,
      display,
      wifi,
      rgb,
      remap,
    });
  }, []);

  useEffect(() => {
    // Steam overlays can temporarily hide Quick Access. Visibility controls
    // polling only; section navigation belongs to the explicit links/back button.
    if (!visible || activeSection) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [activeSection, refresh, visible]);

  if (!activeSection) return <PageShell>
    <PanelSection title="Hardware Controls">
      <SectionLink title="TDP" description={tdpSummary(overview.tdp)} onClick={() => setActiveSection("tdp")} />
      <SectionLink title="Vibration" description={vibrationSummary(overview)} onClick={() => setActiveSection("vibration")} />
      <SectionLink title="RGB Lighting" description={rgbSummary(overview.rgb)} onClick={() => setActiveSection("rgb")} />
      <SectionLink title="Button Remapper" description={remapSummary(overview.remap)} onClick={() => setActiveSection("remap")} />
      <SectionLink title="OLED Display" description={displaySummary(overview.display)} onClick={() => setActiveSection("display")} />
      <SectionLink title="WiFi" description={wifiSummary(overview.wifi)} onClick={() => setActiveSection("wifi")} />
    </PanelSection>
    <PanelSection title="Device">
      <PanelSectionRow>
        <Field label="Lenovo Legion Go 2" description="OLED · SteamOS · Z2 Extreme support" />
      </PanelSectionRow>
    </PanelSection>
    <PanelSection title="Plugin">
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
        : "About";

  return <PageShell>
    <SectionHeader title={title} onBack={() => setActiveSection(null)} />
    {activeSection === "tdp" && <TdpPage />}
    {activeSection === "vibration" && <VibrationPage />}
    {activeSection === "display" && <DisplayPage />}
    {activeSection === "wifi" && <WifiPage />}
    {activeSection === "rgb" && <RgbPage />}
    {activeSection === "remap" && <RemapPage />}
    {activeSection === "about" && <>
      <PanelSection title="Legion Go 2 Companion">
        <PanelSectionRow>
          <Field label={`Version ${overview.version}`} description="All-in-one hardware controls for Decky Loader." />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Included modules" description="LeGoTDP 1.7.0 · LeGo Vibe Control 1.5.0 · LeGo2 Brightness Fix 2.0.0 · WiFi Optimizer Go 2 0.13.2 · RGB Lighting 1.0.0 · Button Remapper 1.0.0" />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Rayek" description="BSD-3-Clause open-source plugin. Vibration portions also retain their MIT notice." />
        </PanelSectionRow>
      </PanelSection>
      <PanelSection title="Updates">
        <PanelSectionRow>
          <Field label="Development build" description="Automatic updates will be enabled after the combined plugin has its own release repository." />
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
        description="TDP, CPU controls, vibration, OLED, WiFi, RGB and button remapping are unavailable while a standalone plugin is installed." /></PanelSectionRow>
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
  return <Controls />;
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
  guardStatus = null;
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
      watchersEnabled = false;
      removeEventListener("companion_guard", listener);
      guardListeners.clear();
      guardStatus = null;
      stopTdpWatcher();
      stopVibrationWatcher();
    },
  };
});
