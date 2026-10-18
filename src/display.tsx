// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGo2BrightnessFix

import { callable, toaster, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { confirmAction, detailsRows, errorText, infoRow, statusPlaceholder } from "./shared";

type PanelMode = "gamma22" | "pq" | "hybrid";

// One upside and one downside each, because that is the whole decision. The
// numbers come from this panel: a ~471 nit ceiling on the eDP AUX luminance
// control, against the 1100 nit peak the EDID advertises for a 10% window.
const MODE_INFO: Record<PanelMode, { label: string; pro: string; con: string }> = {
  gamma22: {
    label: "Gamma 2.2",
    pro: "The system brightness slider drives the panel directly, so black stays black at any level.",
    con: "HDR is tone mapped against the panel's ~471 nit ceiling, so bright highlights lose their punch.",
  },
  pq: {
    label: "PQ",
    pro: "HDR reaches the panel's full 1100 nit peak, at the absolute levels the content was mastered for.",
    con: "The panel ignores its brightness control, so dimming only fades the image and shadows wash out to grey.",
  },
  hybrid: {
    label: "Hybrid",
    pro: "Gamma 2.2 for everyday use and PQ only while a game is presenting HDR, so neither one is given up.",
    con: "Each switch between HDR and SDR briefly blanks the screen.",
  },
};

// Hybrid first and recommended: it is the only one that does not trade away
// something the user would notice every day.
const RECOMMENDED: PanelMode = "hybrid";
const MODE_ORDER: PanelMode[] = ["hybrid", "pq", "gamma22"];

const modeLabel = (m: PanelMode) =>
  m === RECOMMENDED ? `${MODE_INFO[m].label} (recommended)` : MODE_INFO[m].label;

interface State {
  native_support_detected?: boolean;
  native_support_reason?: string;
  native_cleanup_pending?: boolean;
  native_cleanup_error?: string;
  native_restart_pending?: boolean;
  // Setup gate
  panel_mode: PanelMode | null;
  active_mode: PanelMode | null;
  setup_done: boolean;
  setup_note: string;
  setup_error: string;
  settings_error?: string;
  restart_pending: boolean;
  restart_error: string;
  reset_in_progress?: boolean;
  reset_restart_pending?: boolean;
  reset_error?: string;
  reset_note?: string;
  // Hybrid half
  hybrid_reason: string;
  hdr_now: boolean;
  gamescope_reachable: boolean;
  // Brightness half
  panel_ok: boolean;
  panel_desc: string;
  backlight: string;
  enabled: boolean;
  active: boolean;
  reason: string;
  nits: number;
  max_nits: number;
  // EDID half
  edid_fix: boolean;
  edid_patched: boolean;
  edid_reason: string;
  edid_game_nits: number;
}

const nativeFixBlocked = (state: State | null) =>
  state?.native_support_detected === true || state?.native_cleanup_pending === true;

const getState = callable<[], State>("display_get_state");
const setEnabled = callable<[boolean], State>("display_set_enabled");
const setEdidFix = callable<[boolean], State>("display_set_edid_fix");
const runSetup = callable<[string], State>("display_run_setup");
const setPanelMode = callable<[string], State>("display_set_panel_mode");
const restartSession = callable<[], State>("display_restart_session");
const resetDisplaySettings = callable<[], State>("display_reset_settings");
const notify = (body: string) =>
  toaster.toast({ title: "Display", body, duration: 5000 });

// ── Mode picker ────────────────────────────────────────────────────────────────
const ProCon: FC<{ mode: PanelMode }> = ({ mode }) => (
  <span style={{ display: "block", lineHeight: 1.35 }}>
    <span style={{ display: "block", color: "#5ee07a" }}>{`+ ${MODE_INFO[mode].pro}`}</span>
    <span style={{ display: "block", color: "#ff7b72" }}>{`- ${MODE_INFO[mode].con}`}</span>
  </span>
);

/** One button; the two lines that justify it are its description, so they cost no focus stop. */
const ModeChoice: FC<{
  mode: PanelMode;
  busy: boolean;
  onPick: (m: PanelMode) => void;
  label?: string;
}> = ({ mode, busy, onPick, label }) => (
  <PanelSectionRow>
    <ButtonItem layout="below" onClick={() => onPick(mode)} disabled={busy} description={<ProCon mode={mode} />}>
      {label ?? modeLabel(mode)}
    </ButtonItem>
  </PanelSectionRow>
);

// ── Main content ───────────────────────────────────────────────────────────────
export const DisplayPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const [state, setState] = useState<State | null>(null);
  const [statusError, setStatusError] = useState("");
  const [busy, setBusy] = useState(false);
  // Both lists start folded away: on first run the recommendation is the whole
  // point, and afterwards the mode is a decision already made.
  const [showOthers, setShowOthers] = useState(false);
  const [showSwitch, setShowSwitch] = useState(false);
  const [details, setDetails] = useState(false);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const writing = useRef(false);
  const readInFlight = useRef(false);
  const refreshAfterRead = useRef(false);
  const readRevision = useRef(0);
  const latestState = useRef<State | null>(null);
  const statusTrusted = useRef(false);
  const visitRevision = useRef(0);
  if (isVisible.current !== visible) {
    statusTrusted.current = false;
    visitRevision.current += 1;
  }
  const acceptState = useCallback((next: State) => {
    latestState.current = next;
    statusTrusted.current = isVisible.current;
    setStatusError("");
    setState(next);
  }, []);
  const acceptWriteState = useCallback((next: State, visit: number) => {
    if (mounted.current && visit === visitRevision.current) acceptState(next);
  }, [acceptState]);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || writing.current || readInFlight.current) return;
    readInFlight.current = true;
    const revision = readRevision.current;
    try {
      const next = await getState();
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        acceptState(next);
        setStatusError("");
      }
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        statusTrusted.current = false;
        setStatusError("Display status is unavailable. Retrying while this page is open.");
      }
    } finally {
      readInFlight.current = false;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refresh(); }
    }
  }, [acceptState]);

  // Only poll while the panel is actually on screen. The backend keeps working
  // either way - this is just what the user sees.
  useEffect(() => {
    if (!visible) return;
    if (readInFlight.current) refreshAfterRead.current = true;
    void refresh();
    const id = setInterval(refresh, 1000);
    return () => { clearInterval(id); readRevision.current += 1; };
  }, [visible, refresh]);

  // A confirmation takes focus from the Quick Access menu, so its OK usually runs while the
  // panel is hidden and its status untrusted; it acts on what the dialog showed.
  const beginWrite = (confirmed = false) => {
    if (!mounted.current || writing.current || (!confirmed && (!isVisible.current || !statusTrusted.current))) return false;
    writing.current = true; readRevision.current += 1; setBusy(true);
    return true;
  };
  const finishWrite = () => {
    writing.current = false;
    if (mounted.current) setBusy(false);
    if (!statusTrusted.current) {
      if (readInFlight.current) refreshAfterRead.current = true;
      else void refresh();
    }
  };
  const failedWrite = () => {
    statusTrusted.current = false;
    if (mounted.current) setStatusError("Display status is unavailable. Confirming the result of the previous change.");
  };

  const toggle = useCallback(
    async (fn: (v: boolean) => Promise<State>, key: keyof State, value: boolean) => {
      if (key === "enabled" && nativeFixBlocked(latestState.current)) return;
      if (!beginWrite()) return;
      const visit = visitRevision.current;
      if (latestState.current) acceptState({ ...latestState.current, [key]: value });
      let failed = false;
      try {
        const next = await fn(value);
        acceptWriteState(next, visit);
      } catch {
        failed = true;
        failedWrite();
      } finally {
        finishWrite();
        if (failed) void refresh();
      }
    },
    [acceptState, acceptWriteState, refresh],
  );

  // First setup and later mode changes share one write path; only the RPC and the failure title differ.
  const writeMode = useCallback(async (write: (mode: string) => Promise<State>, mode: PanelMode, confirmed = false) => {
    if (nativeFixBlocked(latestState.current)) return;
    if (!beginWrite(confirmed)) return;
    const visit = visitRevision.current;
    let failed = false;
    try {
      const next = await write(mode);
      acceptWriteState(next, visit);
      if (next.setup_error) notify(next.setup_error);
    } catch (e) {
      failed = true;
      failedWrite();
      notify(errorText(e));
    } finally {
      finishWrite();
      if (failed) void refresh();
    }
  }, [acceptWriteState, refresh]);
  const setup = (mode: PanelMode) => writeMode(runSetup, mode, true);
  const changeMode = (mode: PanelMode) => writeMode(setPanelMode, mode);

  const restart = useCallback(async () => {
    if (!beginWrite(true)) return;
    const visit = visitRevision.current;
    try {
      // If this returns at all, the session did not go down - so surface
      // whatever the backend reported instead of leaving a dead button.
      const next = await restartSession();
      acceptWriteState(next, visit);
    } catch {
      failedWrite();
      /* the session going down mid-call is the expected outcome */
    } finally {
      finishWrite();
    }
  }, [acceptWriteState]);

  const reset = useCallback(async () => {
    if (nativeFixBlocked(latestState.current)) return;
    if (!beginWrite(true)) return;
    const visit = visitRevision.current;
    let failed = false;
    try {
      const next = await resetDisplaySettings();
      if (mounted.current) {
        acceptWriteState(next, visit);
        if (!next.reset_error && visit === visitRevision.current) {
          setShowOthers(false); setShowSwitch(false);
        }
      }
      if (next.reset_error) notify(next.reset_error);
    } catch (error) {
      failed = true;
      failedWrite();
      notify(errorText(error));
    } finally { finishWrite(); if (failed) void refresh(); }
  }, [acceptWriteState, refresh]);

  const confirmRestart = () => confirmAction("Restart Gaming Mode?",
    "Steam and any running game will close and reopen. Save your game first.", "Restart", () => void restart());
  const confirmSetup = (mode: PanelMode) => confirmAction(`Install display fix for ${MODE_INFO[mode].label}?`,
    "If you already have a display script for this screen, it's kept as a .backup file. Gaming Mode needs a restart afterwards.",
    "Install", () => void setup(mode));
  const confirmReset = () => confirmAction("Reset display fix?",
    "Companion removes its display changes and restores your previous display script, if there was one. Gaming Mode may need a restart.",
    "Reset", () => void reset());

  if (!state || !statusTrusted.current)
    return statusPlaceholder({ label: "Display status unavailable", error: statusError, onRetry: () => void refresh() });

  if (state.settings_error) return <PanelSection title="Display settings unavailable">
    {infoRow("Display controls are paused", state.settings_error)}
  </PanelSection>;

  if (state.reset_in_progress) return <PanelSection title="Finish display reset">
    {infoRow("Display controls are paused",
      state.reset_error || "An earlier reset did not finish. Retry to restore the display and remove the Companion script.")}
    <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={reset}>Retry display reset</ButtonItem></PanelSectionRow>
  </PanelSection>;

  const edidToggle = <PanelSectionRow>
    <ToggleField
      label="Fix HDR brightness in games"
      description={`Games see the screen's real HDR brightness. Restart the game after changing.${state.edid_patched
        ? state.edid_game_nits > 0 ? ` Games see ${state.edid_game_nits.toFixed(0)} nits.` : ""
        : state.edid_fix ? " Not active yet." : ""}`}
      checked={state.edid_fix}
      disabled={busy}
      onChange={(v) => toggle(setEdidFix, "edid_fix", v)}
    />
  </PanelSectionRow>;
  const restartRow = (label: string) => <>
    {infoRow(label, state.restart_error
      ? `${state.restart_error}. Restart from the Steam menu (Power → Switch to Desktop and back) or reboot.`
      : "Steam closes and reopens.")}
    <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={confirmRestart}>Restart Gaming Mode</ButtonItem></PanelSectionRow>
  </>;
  // Never disabled: it sits just above the last row, and reset() refuses while a write runs.
  const resetRow = <PanelSectionRow><ButtonItem layout="below" onClick={confirmReset}
    description={state.reset_error || "Removes Companion's display changes and restores your previous script."}>
    Reset display fix
  </ButtonItem></PanelSectionRow>;
  const detailsPanel = detailsRows(details, () => setDetails(value => !value), [
    ...(state.native_support_reason ? [["SteamOS support", state.native_support_reason] as [string, string]] : []),
    ["Panel", `${state.panel_desc || "Unknown panel"}${state.backlight ? ` · backlight ${state.backlight}` : ""}${
      state.max_nits ? ` · up to ${state.max_nits.toFixed(0)} nits` : ""}`],
    ["Brightness", state.reason || "—"],
    ["HDR info", state.edid_reason || "—"],
  ]);
  const switchRows = (onPick: (mode: PanelMode) => void) => <>
    {showSwitch && MODE_ORDER.filter((m) => m !== state.panel_mode).map((m) => (
      <ModeChoice key={m} mode={m} label={`Use ${MODE_INFO[m].label}`} busy={busy} onPick={onPick} />
    ))}
  </>;

  if (nativeFixBlocked(state)) return <PanelSection title="Display">
    {infoRow(state.native_support_detected ? "SteamOS supports this screen" : "Display fix cleanup pending",
      state.native_support_detected
        ? "Companion's display fix is off. The HDR game fix below still works."
        : "An earlier handover to SteamOS display support still needs cleanup.")}
    {state.native_cleanup_error
      ? infoRow("Display fix cleanup needs attention", `${state.native_cleanup_error} Companion will retry automatically.`)
      : state.native_cleanup_pending && infoRow("Removing the Companion display fix",
        "Cleanup has not finished. Companion will retry automatically.")}
    {state.native_restart_pending && restartRow("Restart Gaming Mode to finish cleanup")}
    {edidToggle}
    {detailsPanel}
  </PanelSection>;

  // The mode and brightness controls need the Companion display script.
  // The HDR game fix stays available independently of this setup.
  if (!state.panel_mode) {
    return (
      <PanelSection title="Display">
        {state.reset_restart_pending && restartRow("Restart Gaming Mode to finish the reset")}
        {infoRow("Choose a display mode", state.setup_error || "You can change it later.")}
        <ModeChoice mode={RECOMMENDED} busy={busy || !!state.reset_restart_pending} onPick={confirmSetup} />
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => setShowOthers((v) => !v)}
            disabled={busy || !!state.reset_restart_pending}
          >
            {showOthers ? "Hide other options" : "Other options"}
          </ButtonItem>
        </PanelSectionRow>
        {showOthers &&
          MODE_ORDER.filter((m) => m !== RECOMMENDED).map((m) => (
            <ModeChoice key={m} mode={m} busy={busy || !!state.reset_restart_pending} onPick={confirmSetup} />
          ))}
        {edidToggle}
        {detailsPanel}
      </PanelSection>
    );
  }

  if (!state.setup_done) {
    return (
      <PanelSection title="Display">
        {infoRow(`Display script needed (${MODE_INFO[state.panel_mode].label})`,
          state.setup_error || state.setup_note || "Install it to use this mode.")}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => confirmSetup(state.panel_mode as PanelMode)}
            disabled={busy}
          >
            {busy ? "Installing…" : "Install display fix"}
          </ButtonItem>
        </PanelSectionRow>
        {edidToggle}
        {resetRow}
        {detailsPanel}
      </PanelSection>
    );
  }

  const modeNow = state.panel_mode === "hybrid"
    ? `Now: Hybrid${state.gamescope_reachable ? ` (screen in ${state.hdr_now ? "PQ" : "Gamma 2.2"})` : ""}`
    : `Now: ${MODE_INFO[state.panel_mode].label}`;
  const changeButton = <PanelSectionRow>
    <ButtonItem layout="below" description={modeNow} onClick={() => setShowSwitch((v) => !v)} disabled={busy}>
      {showSwitch ? "Cancel" : "Change display mode"}
    </ButtonItem>
  </PanelSectionRow>;

  if (state.restart_pending) {
    return (
      <PanelSection title="Display">
        {restartRow("Restart Gaming Mode to finish")}
        {changeButton}
        {switchRows(changeMode)}
        {edidToggle}
        {resetRow}
        {detailsPanel}
      </PanelSection>
    );
  }

  return (
    <PanelSection title="Display">
      {changeButton}
      {switchRows((picked) => { setShowSwitch(false); changeMode(picked); })}
      {state.panel_ok && <PanelSectionRow>
        <ToggleField
          label="Brightness slider in HDR"
          description={`Lets the Steam brightness slider dim the screen in PQ.${!state.enabled ? ""
            : state.active ? ` Working now (${state.nits.toFixed(0)} nits).` : " Not needed in the current mode."}`}
          checked={state.enabled}
          disabled={busy}
          onChange={(v) => toggle(setEnabled, "enabled", v)}
        />
      </PanelSectionRow>}
      {edidToggle}
      {resetRow}
      {detailsPanel}
    </PanelSection>
  );
};
