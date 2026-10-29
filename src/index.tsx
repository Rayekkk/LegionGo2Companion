// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { addEventListener, removeEventListener, definePlugin, toaster, useQuickAccessVisible } from "@decky/api";
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
import { FanPage, fanSummary } from "./fan";
import { VibrationPage, startVibrationWatcher, stopVibrationWatcher } from "./vibration";
import { WifiPage, wifiSummary } from "./wifi";
import { RgbPage, rgbSummary } from "./rgb";
import { RemapPage, remapSummary } from "./remap";
import { BatteryPage, batterySummary } from "./battery";
import { ControllerPage, controllerSummary } from "./controller";
import { DeviceReportSection, UpdateSection, startUpdates, stopUpdates } from "./updates";
import { FirmwarePanel } from "./firmware";
import { errorText, infoRow, rpc } from "./shared";
import { configureOverviewModules, currentOverview, displaySummary, EMPTY_MODULES, guardOverview, invalidateOverviewReads,
  moduleEnabled, type ModuleKey, type ModuleStates, type Overview, overviewDescription, overviewNote, pauseOverview,
  PLUGIN_VERSION, pollOverview, resetOverview, tdpSummary, vibrationSummary, watchOverview } from "./overview";
import { strengthOnly } from "../desktop/native/vibration.mjs";
import { betaNoticeText, betaNoticeTitle, cleanupRetryShown, consoleDetecting, MODULE_LABELS, moduleChecked, moduleLocked, moduleNote, moduleShown, moduleStartingText, startRetryShown } from "../desktop/native/modules.mjs";

type SectionKey = ModuleKey | "about" | "modules";
// What applies to this console besides the modules (get_version.device); a missing value is shown.
type Device = {console?: string | null; model?: string | null; firmware?: boolean; power_led?: boolean; controller_driver?: boolean};
// Key order is the menu and Manage Modules order.
const moduleLabels: Record<ModuleKey, string> = MODULE_LABELS;
const setModuleEnabled = rpc<[ModuleKey, boolean], ModuleStates>("modules_set_enabled");

interface GuardStatus {
  modules?: ModuleStates;
  device?: Device;
  version: string;
  standalone_plugins?: string[];
  blocked: boolean;
  latched?: boolean;
  restart_required?: boolean;
  guard_error?: string;
  message?: string;
}
const getVersion = rpc<[], GuardStatus>("get_version");
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
  pauseOverview();
  guardListeners.forEach(listener => listener({version: PLUGIN_VERSION, blocked: true,
    guard_error: "Could not verify installed plugins. Check the Decky connection and try again."}));
};
const updateGuard = (status: GuardStatus) => {
  if (!frontendActive) return;
  guardRevision += 1;
  if (!validGuard(status)) { pauseUnavailableGuard(); return; }
  // A late response must never unlock a latched conflict in this load; a transient scan error may clear.
  if (guardStatus?.latched && !status.latched) return;
  guardStatus = status;
  guardOverview(status.blocked, status.modules ?? EMPTY_MODULES, status.version);
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

const ModulesPage: FC<{ modules: ModuleStates; device: Device }> = ({ modules, device }) => {
  const [busy, setBusy] = useState<ModuleKey | null>(null);
  // The failed control shows its error until the next action.
  const [error, setError] = useState<{key: ModuleKey; text: string} | null>(null);
  const writing = useRef(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const change = async (name: ModuleKey, enabled: boolean) => {
    if (!mounted.current || writing.current) return;
    writing.current = true; setBusy(name); setError(null);
    try {
      const next = await setModuleEnabled(name, enabled);
      if (guardStatus) updateGuard({...guardStatus, modules: next});
    } catch (failure) {
      if (mounted.current) fail(name, errorText(failure, "The module change could not be confirmed."));
      void refreshGuard();
    } finally { writing.current = false; if (mounted.current) setBusy(null); }
  };
  const fail = (key: ModuleKey, text: string) => {
    setError({key, text});
    toaster.toast({title: moduleLabels[key], body: text});
  };
  return <PanelSection title="Manage Modules">
    {infoRow("Choose your modules", "Turning a module off stops it and hides it. Its settings are kept.")}
    {(Object.keys(moduleLabels) as ModuleKey[]).filter(name => moduleShown(modules[name])).map(name => <PanelSectionRow key={name}>
      <ToggleField label={moduleLabels[name]} checked={moduleChecked(modules[name])} disabled={busy !== null || moduleLocked(modules[name])}
        description={busy === name ? "Applying…" : (error?.key === name && error.text) || moduleNote(name, modules[name], device)}
        onChange={enabled => void change(name, enabled)} />
      {cleanupRetryShown(modules[name]) && <ButtonItem layout="below" disabled={busy !== null} onClick={() => void change(name, false)}>Retry {moduleLabels[name]} cleanup</ButtonItem>}
      {startRetryShown(modules[name]) &&
        <ButtonItem layout="below" disabled={busy !== null} onClick={() => void change(name, true)}>Retry {moduleLabels[name]} start</ButtonItem>}
    </PanelSectionRow>)}
  </PanelSection>;
};

const Controls: FC<{modules?: ModuleStates; device?: Device}> = ({modules = EMPTY_MODULES, device = {}} = {}) => {
  const visible = useQuickAccessVisible();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const [onScreen, setOnScreen] = useState(true);
  const pageVisible = visible && onScreen; // QAM open and the Companion tab actually shown
  const panelOnScreen = () => !panelRef.current || panelRef.current.getClientRects().length > 0;
  const [activeSection, setActiveSection] = useState<SectionKey | null>(null);
  const [overview, setOverview] = useState<Overview>(currentOverview);
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
  // An already-running read may finish while QAM is hidden: keep the mounted view in sync without reading more.
  useEffect(() => watchOverview(setOverview), []);
  // get_version supplies a fresh modules object on every guard check. Depend
  // only on effective enablement, not object identity, to preserve pending reads.
  const enabledModules = (Object.keys(moduleLabels) as ModuleKey[])
    .map(key => moduleEnabled(modules, key) ? "1" : "0").join("");
  useEffect(() => {
    configureOverviewModules(modules);
    setOverview(currentOverview());
  }, [enabledModules]);
  useEffect(() => {
    if (activeSection && activeSection !== "controller" && activeSection in moduleLabels && !moduleEnabled(modules, activeSection as ModuleKey)) setActiveSection(null);
  }, [modules, activeSection]);

  useEffect(() => {
    // Steam overlays can temporarily hide Quick Access. Visibility controls
    // polling only; section navigation belongs to the explicit links/back button.
    if (activeSection) { invalidateOverviewReads(); return; }
    if (!visible || !onScreen) return;
    return pollOverview(setOverview, panelOnScreen, moduleEnabled(modules, "fan"));
  }, [activeSection, enabledModules, visible, onScreen]);

  // Controller battery levels come from the Gyro & touchpad controllers (not the Go S or a Go 1 on older firmware).
  const controllerBatteries = moduleShown(modules.controller);
  // A module still starting keeps its place, without controls, until its start has finished.
  const startingRow = (module: ModuleKey) => {
    const text = moduleStartingText(modules[module]);
    return text ? infoRow(moduleLabels[module], text) : null;
  };
  const summary = (module: ModuleKey, description: string) => ({
    description: overviewDescription(overview, module, description), statusNote: overviewNote(overview, module),
  });

  if (!activeSection) return <PageShell rootRef={panelRef}>
    <PanelSection title="Hardware Controls">
      {betaNoticeTitle(device) && infoRow(betaNoticeTitle(device), betaNoticeText(device))}
      {Object.values(modules).some(state => state?.maintenance) && infoRow("Controller controls paused", "Firmware maintenance is in progress. Saved controller settings have not been changed.")}
      {moduleEnabled(modules, "tdp") && <SectionLink section="tdp" restoreFocus={focusSection === "tdp"} title={moduleLabels.tdp} {...summary("tdp", tdpSummary(overview.tdp))} onClick={() => openSection("tdp")} />}{startingRow("tdp")}
      {moduleEnabled(modules, "fan") && <SectionLink section="fan" restoreFocus={focusSection === "fan"} title={moduleLabels.fan} {...summary("fan", fanSummary(overview.fan))} onClick={() => openSection("fan")} />}{startingRow("fan")}
      {moduleEnabled(modules, "vibration") && <SectionLink section="vibration" restoreFocus={focusSection === "vibration"} title={moduleLabels.vibration} {...summary("vibration", vibrationSummary(overview, strengthOnly(device)))} onClick={() => openSection("vibration")} />}{startingRow("vibration")}
      {moduleEnabled(modules, "rgb") && <SectionLink section="rgb" restoreFocus={focusSection === "rgb"} title={moduleLabels.rgb} {...summary("rgb", rgbSummary(overview.rgb, device.power_led !== false))} onClick={() => openSection("rgb")} />}{startingRow("rgb")}
      {moduleEnabled(modules, "remap") && <SectionLink section="remap" restoreFocus={focusSection === "remap"} title={moduleLabels.remap} {...summary("remap", remapSummary(overview.remap))} onClick={() => openSection("remap")} />}{startingRow("remap")}
      {startingRow("controller")}
      {(moduleEnabled(modules, "controller") || device.firmware !== false) && <SectionLink section="controller" restoreFocus={focusSection === "controller"}
        title={moduleEnabled(modules, "controller") ? moduleLabels.controller : "Controller firmware"}
        {...(moduleEnabled(modules, "controller") ? summary("controller", controllerSummary(overview.controller))
          : { description: "Controller update status and Lenovo firmware updates" })}
        onClick={() => openSection("controller")} />}
      {moduleEnabled(modules, "battery") && <SectionLink section="battery" restoreFocus={focusSection === "battery"} title={moduleLabels.battery} {...summary("battery", batterySummary(overview.battery, controllerBatteries ? overview.controller_batteries : undefined))} onClick={() => openSection("battery")} />}{startingRow("battery")}
      {moduleEnabled(modules, "display") && <SectionLink section="display" restoreFocus={focusSection === "display"} title={moduleLabels.display} {...summary("display", displaySummary(overview.display))} onClick={() => openSection("display")} />}{startingRow("display")}
      {moduleEnabled(modules, "wifi") && <SectionLink section="wifi" restoreFocus={focusSection === "wifi"} title={moduleLabels.wifi} {...summary("wifi", wifiSummary(overview.wifi))} onClick={() => openSection("wifi")} />}{startingRow("wifi")}
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

  const title = activeSection === "modules" ? "Manage Modules"
    : activeSection === "about" ? "About"
    : activeSection === "controller" && !moduleEnabled(modules, "controller") ? "Controller firmware"
    : moduleLabels[activeSection];

  return <PageShell rootRef={panelRef}>
    <SectionHeader title={title} onBack={() => setActiveSection(null)} />
    {activeSection === "modules" && <ModulesPage modules={modules} device={device} />}
    {activeSection === "tdp" && <TdpPage visible={onScreen} />}
    {activeSection === "fan" && <FanPage viewVisible={onScreen} isViewVisible={panelOnScreen} />}
    {activeSection === "vibration" && <VibrationPage strengthOnly={strengthOnly(device)} />}
    {activeSection === "display" && <DisplayPage visible={onScreen} />}
    {activeSection === "wifi" && <WifiPage visible={onScreen} />}
    {activeSection === "rgb" && <RgbPage visible={onScreen} powerLight={device.power_led !== false} />}
    {activeSection === "remap" && <RemapPage visible={onScreen} />}
    {activeSection === "controller" && (moduleEnabled(modules, "controller") ? <ControllerPage visible={pageVisible} firmware={device.firmware !== false} />
      : <FirmwarePanel visible={pageVisible} allowUpdates={!Object.values(modules).some(state => state?.maintenance)} />)}
    {activeSection === "battery" && <BatteryPage visible={onScreen} controllers={controllerBatteries} />}
    {activeSection === "about" && <>
      <PanelSection title="Legion Go 2 Companion">
        {infoRow(`Version ${overview.version}`, "All-in-one hardware controls for Decky Loader.")}
        {infoRow("Author", "Rayek · BSD-3-Clause open-source plugin. Vibration portions also retain their MIT notice.")}
      </PanelSection>
      <UpdateSection currentVersion={overview.version} />
      <DeviceReportSection />
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

  // The backend answers get_version before it has detected the console (applicable null): wait, don't flash every module.
  if (!status || (!status.blocked && consoleDetecting(status.modules))) return <PageShell><PanelSection title="Checking installed plugins">
    {infoRow("Checking compatibility", "Hardware controls will appear after the check completes.")}
  </PanelSection></PageShell>;
  if (status.blocked) return <PageShell>
    <PanelSection title="Companion paused">
      {infoRow("All modules are paused",
        "All hardware controls, including gyro diagnostics and battery protection, are unavailable while a standalone plugin is installed.")}
      {!!status.standalone_plugins?.length && infoRow("Installed standalone plugins", status.standalone_plugins.join(", "))}
      {infoRow(status.restart_required ? "Ready for a restart" : "How to resume", status.guard_error || (status.restart_required
        ? "Companion stays paused for safety and no conflicting plugin is installed now. Restart Decky or the console to start Companion safely."
        : "In Decky Settings, uninstall the plugins listed above and keep their saved settings. Then restart Decky or the console. Disabling a plugin is not enough; it must be uninstalled."))}
      {infoRow("Your settings are kept", "This safeguard does not erase Companion profiles or the standalone settings available for import.")}
      <PanelSectionRow><ButtonItem layout="below" onClick={() => void refreshGuard()}>Check again</ButtonItem></PanelSectionRow>
    </PanelSection>
    {status.device?.firmware !== false && <FirmwarePanel visible={visible} allowUpdates={false} />}
  </PageShell>;
  return <Controls modules={status.modules} device={status.device} />;
};

const Icon: FC = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
    style={{ width: "1em", height: "1em" }}>
    <path d="M7 8h10a5 5 0 0 1 4.7 6.7l-1.1 3.1a2 2 0 0 1-3.3.8L15 16H9l-2.3 2.6a2 2 0 0 1-3.3-.8l-1.1-3.1A5 5 0 0 1 7 8Z" />
    <circle cx="8" cy="12" r="2" />
    <circle cx="16" cy="12" r="2" />
  </svg>
);

// A plugin load or unload discards the previous session's guard and overview state.
const resetSession = (active: boolean) => {
  guardRevision += 1;
  guardRead = null;
  guardStatus = null;
  resetOverview(active);
};

export default definePlugin(() => {
  startUpdates();
  frontendActive = true;
  resetSession(true);
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
      resetSession(false);
      tdpWatcherEnabled = vibeWatcherEnabled = false;
      removeEventListener("companion_guard", listener);
      guardListeners.clear();
      stopTdpWatcher();
      stopVibrationWatcher();
    },
  };
});
