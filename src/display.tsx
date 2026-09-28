// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGo2BrightnessFix

import { callable, toaster, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  Spinner,
  ToggleField,
} from "@decky/ui";
import { FC, Fragment, useCallback, useEffect, useRef, useState } from "react";

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
const notify = (title: string, body: string) =>
  toaster.toast({ title, body, duration: 5000 });

// ── Mode picker ────────────────────────────────────────────────────────────────
const ProCon: FC<{ mode: PanelMode }> = ({ mode }) => (
  <div style={{ fontSize: "0.75em", lineHeight: 1.35, padding: "0 16px 10px" }}>
    <div style={{ color: "#5ee07a" }}>{`+ ${MODE_INFO[mode].pro}`}</div>
    <div style={{ color: "#ff7b72" }}>{`- ${MODE_INFO[mode].con}`}</div>
  </div>
);

/** One button plus the two lines that justify it. */
const ModeChoice: FC<{
  mode: PanelMode;
  busy: boolean;
  onPick: (m: PanelMode) => void;
}> = ({ mode, busy, onPick }) => (
  <Fragment>
    <PanelSectionRow>
      <ButtonItem layout="below" onClick={() => onPick(mode)} disabled={busy}>
        {modeLabel(mode)}
      </ButtonItem>
    </PanelSectionRow>
    <PanelSectionRow>
      <ProCon mode={mode} />
    </PanelSectionRow>
  </Fragment>
);

// ── Main content ───────────────────────────────────────────────────────────────
export const DisplayPage: FC = () => {
  const visible = useQuickAccessVisible();
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState(false);
  // Both lists start folded away: on first run the recommendation is the whole
  // point, and afterwards the mode is a decision already made.
  const [showOthers, setShowOthers] = useState(false);
  const [showSwitch, setShowSwitch] = useState(false);
  const [showReset, setShowReset] = useState(false);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const writing = useRef(false);
  const readInFlight = useRef(false);
  const readRevision = useRef(0);
  const latestState = useRef<State | null>(null);
  const acceptState = useCallback((next: State) => {
    latestState.current = next;
    setState(next);
  }, []);
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
      if (mounted.current && isVisible.current && revision === readRevision.current) acceptState(next);
    } catch {
      /* backend not up yet; the next tick will pick it up */
    } finally { readInFlight.current = false; }
  }, [acceptState]);

  // Only poll while the panel is actually on screen. The backend keeps working
  // either way - this is just what the user sees.
  useEffect(() => {
    if (!visible) return;
    void refresh();
    const id = setInterval(refresh, 1000);
    return () => { clearInterval(id); readRevision.current += 1; };
  }, [visible, refresh]);

  const beginWrite = () => {
    if (writing.current) return false;
    writing.current = true; readRevision.current += 1; setBusy(true);
    return true;
  };
  const finishWrite = () => {
    writing.current = false;
    if (mounted.current) setBusy(false);
  };

  const toggle = useCallback(
    async (fn: (v: boolean) => Promise<State>, key: keyof State, value: boolean) => {
      if (key === "enabled" && nativeFixBlocked(latestState.current)) return;
      if (!beginWrite()) return;
      if (latestState.current) acceptState({ ...latestState.current, [key]: value });
      let failed = false;
      try {
        const next = await fn(value);
        if (mounted.current) acceptState(next);
      } catch {
        failed = true;
      } finally {
        finishWrite();
        if (failed) void refresh();
      }
    },
    [acceptState, refresh],
  );

  const setup = useCallback(async (mode: PanelMode) => {
    if (nativeFixBlocked(latestState.current)) return;
    if (!beginWrite()) return;
    try {
      const next = await runSetup(mode);
      if (mounted.current) acceptState(next);
      if (next.setup_error) notify("Setup failed", next.setup_error);
    } catch (e) {
      notify("Setup failed", e instanceof Error ? e.message : String(e));
    } finally {
      finishWrite();
    }
  }, [acceptState]);

  const changeMode = useCallback(async (mode: PanelMode) => {
    if (nativeFixBlocked(latestState.current)) return;
    if (!beginWrite()) return;
    try {
      const next = await setPanelMode(mode);
      if (mounted.current) acceptState(next);
      if (next.setup_error) notify("Could not change mode", next.setup_error);
    } catch (e) {
      notify("Could not change mode", e instanceof Error ? e.message : String(e));
    } finally {
      finishWrite();
    }
  }, [acceptState]);

  const restart = useCallback(async () => {
    if (!beginWrite()) return;
    try {
      // If this returns at all, the session did not go down - so surface
      // whatever the backend reported instead of leaving a dead button.
      const next = await restartSession();
      if (mounted.current) acceptState(next);
    } catch {
      /* the session going down mid-call is the expected outcome */
    } finally {
      finishWrite();
    }
  }, [acceptState]);

  const reset = useCallback(async () => {
    if (nativeFixBlocked(latestState.current)) return;
    if (!beginWrite()) return;
    let failed = false;
    try {
      const next = await resetDisplaySettings();
      if (mounted.current) {
        acceptState(next);
        if (!next.reset_error) {
          setShowOthers(false); setShowSwitch(false); setShowReset(false);
        }
      }
      if (next.reset_error) notify("Reset incomplete", next.reset_error);
    } catch (error) {
      failed = true;
      notify("Reset incomplete", error instanceof Error ? error.message : String(error));
    } finally { finishWrite(); if (failed) void refresh(); }
  }, [acceptState, refresh]);

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Spinner />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (state.settings_error) return <PanelSection title="Display settings unavailable">
    <PanelSectionRow><Field focusable label="Display controls are paused" description={state.settings_error} /></PanelSectionRow>
  </PanelSection>;

  if (state.reset_in_progress) return <PanelSection title="Finish display reset">
    <PanelSectionRow><Field focusable label="Display controls are paused"
      description={state.reset_error || "An earlier reset did not finish. Retry to restore the display and remove the Companion script."} /></PanelSectionRow>
    <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={reset}>Retry Display Reset</ButtonItem></PanelSectionRow>
  </PanelSection>;

  const edidPanel = <PanelSection title="EDID for games">
    <PanelSectionRow>
      <ToggleField
        label="Enabled"
        description="Drop the DisplayID block from the EDID gamescope hands to games, which DXVK cannot parse."
        checked={state.edid_fix}
        disabled={busy}
        onChange={(v) => toggle(setEdidFix, "edid_fix", v)}
      />
    </PanelSectionRow>
    <PanelSectionRow>
      <Field label="Status" description={state.edid_reason} focusable>
        {state.edid_patched ? "Applied" : "Standby"}
      </Field>
    </PanelSectionRow>
    {state.edid_patched && state.edid_game_nits > 0 && <PanelSectionRow>
      <Field label="Games read" focusable
        description="Without this they fall back to DXVK's 1499 nit placeholder and generic primaries.">
        {`${state.edid_game_nits.toFixed(0)} nits`}
      </Field>
    </PanelSectionRow>}
  </PanelSection>;

  if (nativeFixBlocked(state)) return <>
    <PanelSection title="Display fix">
      <PanelSectionRow><Field focusable
        label={state.native_support_detected ? "Native display support detected" : "Display fix cleanup pending"}
        description={state.native_support_reason || (state.native_support_detected
          ? "The system provides the display support previously supplied by Companion."
          : "An earlier handover to native display support still needs cleanup.")} />
      </PanelSectionRow>
      <PanelSectionRow><Field focusable label="Companion display fix disabled"
        description="PQ, Gamma 2.2, Hybrid switching and brightness forwarding are unavailable. EDID correction remains independent below." />
      </PanelSectionRow>
      {state.native_cleanup_error ? <PanelSectionRow><Field focusable label="Display fix cleanup needs attention"
        description={`${state.native_cleanup_error} Companion will retry automatically.`} />
      </PanelSectionRow> : state.native_cleanup_pending && <PanelSectionRow><Field focusable
        label="Removing the Companion display fix"
        description="Cleanup has not finished. Companion will retry automatically; the display fix remains disabled." />
      </PanelSectionRow>}
      {state.native_restart_pending && <>
        <PanelSectionRow><Field focusable label="Restart Gaming Mode to finish cleanup"
          description="Restart Gaming Mode to finish withdrawing the Companion display script from the current session. This closes Steam and brings it back." />
        </PanelSectionRow>
        {state.restart_error && <PanelSectionRow><Field focusable label="Could not restart" description={state.restart_error} /></PanelSectionRow>}
        <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={restart}>Restart Game Mode</ButtonItem></PanelSectionRow>
      </>}
    </PanelSection>
    {edidPanel}
  </>;

  const resetPanel = <PanelSection title="Reset display fix">
    <PanelSectionRow><Field focusable label="Restore previous display handling"
      description="Withdraws brightness, HDR and EDID adjustments, removes the Companion display script and clears this module's settings. A previous third-party script is restored from its backup. Gaming Mode may need a restart." /></PanelSectionRow>
    {state.reset_error && <PanelSectionRow><Field focusable label="Reset incomplete" description={state.reset_error} /></PanelSectionRow>}
    <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => setShowReset(value => !value)}>
      {showReset ? "Cancel Reset" : "Reset Display Fix"}
    </ButtonItem></PanelSectionRow>
    {showReset && <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={reset}>Confirm Display Reset</ButtonItem></PanelSectionRow>}
  </PanelSection>;

  // The mode and brightness controls need the Companion display script.
  // EDID correction stays available independently of this setup.
  if (!state.panel_mode) {
    return (
      <>
      <PanelSection title="Choose a display mode">
        {state.reset_restart_pending && <>
          <PanelSectionRow><Field focusable label="Display fix reset"
            description="Companion controls have been withdrawn. Restart Gaming Mode to unload the display script before choosing a new mode." /></PanelSectionRow>
          {state.restart_error && <PanelSectionRow><Field focusable label="Could not restart" description={state.restart_error} /></PanelSectionRow>}
          <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={restart}>Restart Game Mode</ButtonItem></PanelSectionRow>
        </>}
        <PanelSectionRow>
          <Field
            focusable
            description={
              "How should the Legion Go 2 panel be driven? This installs a " +
              "gamescope display script and replaces any script you already " +
              "have for this panel - the old one is kept as a .backup file. " +
              "You can change your choice at any time."
            }
          />
        </PanelSectionRow>
        <ModeChoice mode={RECOMMENDED} busy={busy || !!state.reset_restart_pending} onPick={setup} />

        {state.setup_error && (
          <PanelSectionRow>
            <Field focusable label="Setup failed" description={state.setup_error} />
          </PanelSectionRow>
        )}

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
            <ModeChoice key={m} mode={m} busy={busy || !!state.reset_restart_pending} onPick={setup} />
          ))}
      </PanelSection>
      {edidPanel}
      </>
    );
  }

  if (!state.setup_done) {
    return (
      <>
      <PanelSection title="Setup">
        <PanelSectionRow>
          <Field
            focusable
            label={`Display script required (${MODE_INFO[state.panel_mode].label})`}
            description={
              "The script for this mode is not in place. Installing it replaces " +
              "any script you already have for this panel - the old one is kept " +
              "alongside it as a .backup file."
            }
          />
        </PanelSectionRow>
        {state.setup_note && (
          <PanelSectionRow>
            <Field focusable label="Status" description={state.setup_note} />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => setup(state.panel_mode as PanelMode)}
            disabled={busy}
          >
            {busy ? "Installing..." : "Setup Display Fix"}
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
      {edidPanel}
      {resetPanel}
      </>
    );
  }

  if (state.restart_pending) {
    return (
      <>
      <PanelSection title="Restart needed">
        <PanelSectionRow>
          <Field
            focusable
            label="Display script installed"
            description={
              "gamescope only reads display scripts when it starts, so Game Mode " +
              "has to restart before anything changes. This closes Steam and " +
              "brings it straight back."
            }
          />
        </PanelSectionRow>
        {state.restart_error && (
          <PanelSectionRow>
            <Field
              focusable
              label="Could not restart"
              description={
                `${state.restart_error}. Restart Game Mode yourself - Steam menu, ` +
                "Power, Switch to Desktop and back, or just reboot."
              }
            />
          </PanelSectionRow>
        )}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={restart} disabled={busy}>
            Restart Game Mode
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => setShowSwitch((v) => !v)}
            disabled={busy}
          >
            {showSwitch ? "Keep Current Choice" : "Change Display Mode"}
          </ButtonItem>
        </PanelSectionRow>
        {showSwitch &&
          MODE_ORDER.filter((m) => m !== state.panel_mode).map((m) => (
            <ModeChoice key={m} mode={m} busy={busy} onPick={changeMode} />
          ))}
      </PanelSection>
      {edidPanel}
      {resetPanel}
      </>
    );
  }

  return (
    <>
      <PanelSection title="Display">
        <PanelSectionRow>
          <Field
            focusable
            label={state.panel_desc || "unknown"}
            description={
              state.backlight
                ? `backlight: ${state.backlight}` +
                  (state.max_nits ? ` · up to ${state.max_nits.toFixed(0)} nits` : "")
                : undefined
            }
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Display mode">
        <PanelSectionRow>
          <Field label="Mode" focusable>
            {MODE_INFO[state.panel_mode].label}
          </Field>
        </PanelSectionRow>

        {state.panel_mode === "hybrid" && (
          <PanelSectionRow>
            <Field label="Panel now" description={state.hybrid_reason} focusable>
              {!state.gamescope_reachable
                ? "Unknown"
                : state.hdr_now ? "PQ" : "Gamma 2.2"}
            </Field>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={() => setShowSwitch((v) => !v)}
            disabled={busy}
          >
            {showSwitch ? "Cancel" : "Switch Display Mode"}
          </ButtonItem>
        </PanelSectionRow>

        {showSwitch &&
          MODE_ORDER.filter((m) => m !== state.panel_mode).map((m) => (
            <ModeChoice
              key={m}
              mode={m}
              busy={busy}
              onPick={(picked) => {
                setShowSwitch(false);
                changeMode(picked);
              }}
            />
          ))}
      </PanelSection>

      <PanelSection title="Brightness slider">
        {state.panel_ok ? (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Enabled"
                description="Forward the Steam brightness slider to gamescope while the panel runs in HDR/PQ."
                checked={state.enabled}
                disabled={busy}
                onChange={(v) => toggle(setEnabled, "enabled", v)}
              />
            </PanelSectionRow>

            <PanelSectionRow>
              <Field label="Status" description={state.reason} focusable>
                {state.active ? "Active" : "Standby"}
              </Field>
            </PanelSectionRow>

            {state.active && (
              <PanelSectionRow>
                <Field label="Level" focusable>
                  {`${state.nits.toFixed(1)} / ${state.max_nits.toFixed(0)} nits`}
                </Field>
              </PanelSectionRow>
            )}
          </>
        ) : (
          <PanelSectionRow>
            <Field
              focusable
              label="Not applicable"
              description={
                "This half only runs on panels known to ignore the backlight in " +
                "HDR/PQ, so nothing is being changed."
              }
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      {edidPanel}
      {resetPanel}
    </>
  );
};
