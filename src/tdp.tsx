// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP

import {
  addEventListener,
  callable,
  removeEventListener,
  toaster,
  useQuickAccessVisible,
} from "@decky/api";
import {
  ButtonItem,
  DropdownItem,
  PanelSection,
  PanelSectionRow,
  SliderField,
  Spinner,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { confirmAction, detailsRows, errorText, infoRow, runningApp, watchRunningApp, type RunningApp } from "./shared";
import {
  absolute, BATTERY_PRESETS_LABEL, BATTERY_PRESETS_NOTE, boostNote, boostUsable, capsState, editorControls, EPP_NAMED_VALUES, EPP_PREFERENCE, eppLabel, eppNote,
  eppPercent, eppTitle, eppUnavailable, eppValue, extrasConfirm, extrasNote, extrasShown, FALLBACK_MAX, FALLBACK_MIN,
  FALLBACK_STD, fromAbsolute, helperRetryShown, limitsText, normalise, offsetMax, powerSummary,
  PRESET_LABELS, PRESET_ORDER, PRESETS, presetNote, resolvedPreset, scopeNote, simpleCaps, simpleTuning, sourceNote,
  shownPresets, sourceText, tdpNotice, boostRoomNote, withFppt, withSpl, withSppt,
  type Caps, type CapsInfo, type CpuPowerControls, type PresetKey, type PresetTable, type TdpInfo, type Tuning,
} from "../desktop/native/tdp.mjs";

// ── Helpers ────────────────────────────────────────────────────────────────────
const toMw  = (w: number)  => w * 1000;
const toW   = (mw: number) => Math.round(mw / 1000);

// The slider model, presets and texts are in desktop/native/tdp.mjs, shared with the Desktop app.

/** Slider view of saved limits (mW). In simple mode a Custom profile follows the single-dial rule. */
function savedTuning(limits: { spl: number; sppt: number; fppt: number }, stored: string | undefined,
                     table: PresetTable, advanced: boolean, caps: Caps, minW: number) {
  const spl = toW(limits.spl), sppt = toW(limits.sppt), fppt = toW(limits.fppt);
  const preset = resolvedPreset(spl, sppt, fppt, stored, table);
  return { preset, tuning: !advanced && preset === "custom" ? simpleTuning(spl, caps, minW) : fromAbsolute(spl, sppt, fppt) };
}

// ── Types ──────────────────────────────────────────────────────────────────────
interface Settings   { spl: number; sppt: number; fppt: number; enabled: boolean;
                       active_preset?: string; advanced_tdp_control?: boolean; separate_battery_presets?: boolean }
interface TdpResult  { success: boolean; stderr?: string; skipped?: boolean; returncode?: number }
interface PowerSource { ac: boolean }
interface GameProfile {
  error?: string;
  exists: boolean;
  // False: the profile is kept but unused; the game runs on Global settings.
  active?: boolean;
  profile: { spl: number; sppt: number; fppt: number; preset?: string };
  ac_separate: boolean;
  ac_profile: { spl: number; sppt: number; fppt: number; ac_preset?: string };
}
interface RunningGame { appId: string; name: string }
interface ReadyState  { ready: boolean; error: string }

// ── Backend callables ──────────────────────────────────────────────────────────
const isReady           = callable<[], ReadyState>("is_ready");
const getSettings       = callable<[], Settings>("get_settings");
const getCaps           = callable<[], CapsInfo>("get_caps");
const applyTdp          = callable<[number, number, number, string, string, string | null], TdpResult>("apply_tdp");
const getTdpInfo        = callable<[], TdpInfo>("get_tdp_info");
const getGameProfile    = callable<[string], GameProfile>("get_game_profile");
const deleteGameProfile = callable<[string], TdpResult>("delete_game_profile");
const setGameProfileActive = callable<[string, boolean], TdpResult>("set_game_profile_active");
const setPluginEnabled  = callable<[boolean], TdpResult>("set_plugin_enabled");
const setPanelActive    = callable<[boolean], void>("set_panel_active");
const setActiveApp      = callable<[string], void>("set_active_app");
const getPowerSource    = callable<[], PowerSource>("get_power_source");
const reapply           = callable<[], TdpResult>("reapply");
const setGameAcProfile  = callable<[string, number, number, number, boolean, string], TdpResult>("set_game_ac_profile");
const retryExtras = callable<[], { success: boolean; error?: string }>("retry_extras");
const getExtrasUnlocked = callable<[], boolean>("get_extras_unlocked");
const setExtrasUnlockedCall = callable<[boolean], TdpResult>("set_extras_unlocked");
const setAdvancedTdpControlCall = callable<[boolean], TdpResult>("set_advanced_tdp_control");
const setSeparateBatteryPresetsCall = callable<[boolean], TdpResult>("set_separate_battery_presets");
const getCpuPowerControls = callable<[string, boolean], CpuPowerControls>("get_cpu_power_controls");
const setCpuBoost       = callable<[boolean, string, boolean, string], CpuPowerControls>("set_cpu_boost");
const setEpp            = callable<[string, string, boolean, string], CpuPowerControls>("set_epp");

// ── Toasts ─────────────────────────────────────────────────────────────────────

const notify = (title: string, body: string) => {
  try {
    toaster.toast({ title, body, duration: 4000 });
  } catch {
    console.error("[legiongo2companion] tdp.notification_failed");
  }
};

const notifyFailure = (title: string, err: unknown) => {
  const body = errorText(err ?? "Unknown error");
  console.error("[legiongo2companion] tdp.operation_failed");
  notify(title, body);
};

// ── Running app watcher ────────────────────────────────────────────────────────

type GameListener = (game: RunningGame | null) => void;
const asGame = (app: RunningApp | null): RunningGame | null =>
  app ? { appId: String(app.appid), name: app.display_name ?? String(app.appid) } : null;

// The backend trusts a frontend-reported appid for 12 seconds. Refresh at half
// that, so one dropped call is not enough to make it fall back to the /proc scan.
const PUSH_INTERVAL_MS = 6000;

/**
 * Tracks the foreground game and tells the backend about it, so the backend's
 * enforce loop applies the right per-game profile even for titles its
 * /proc scan cannot see through pressure-vessel/gamescope.
 *
 * Started at plugin load rather than from the panel: the enforce loop runs
 * whether or not the Quick Access Menu is open, and it is exactly the
 * closed-panel case where the /proc fallback used to guess wrong.
 *
 * Unlike LeGo-Vibe-Control's copy, this pushes on every tick instead of only
 * on a change. The backend trusts a frontend-reported appid for 12 seconds and
 * falls back to the /proc scan once it goes stale, so the value has to be kept
 * fresh, not merely correct at the moment it last changed.
 */
class AppWatcher {
  private static listeners: GameListener[] = [];
  private static current: RunningGame | null = null;
  private static unwatch: (() => void) | undefined;
  private static started = false;
  private static busy = false;
  private static lastPush = 0;
  private static reportFailed = false;
  private static generation = 0;

  static currentGame(): RunningGame | null {
    return this.current;
  }

  static listen(fn: GameListener): () => void {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter((f) => f !== fn);
    };
  }

  static start() {
    if (this.started) return;
    this.started = true;
    this.generation += 1;
    this.current = asGame(runningApp());
    this.unwatch = watchRunningApp(this);
  }

  static stop() {
    this.unwatch?.();
    this.unwatch = undefined;
    this.started = false;
    this.generation += 1;
    this.busy = false;
    this.listeners = [];
    this.current = null;
    this.lastPush = 0;
    this.reportFailed = false;
  }

  // The SMU comes back at firmware defaults after sleep; beat the enforce
  // loop's five-second tick to it.
  static resume() {
    if (!this.started) return;
    void reapply()
      .then((res) => {
        if (!res.success) console.warn("[legiongo2companion] tdp.resume_reapply_failed");
      })
      .catch(() => console.error("[legiongo2companion] tdp.resume_reapply_failed"));
  }

  static async check(app = runningApp()) {
    if (!this.started || this.busy) return;
    const generation = this.generation;
    const game = asGame(app);
    const changed = game?.appId !== this.current?.appId;
    this.current = game;

    // Tick often so a change is noticed quickly, but only send when there is
    // something to say or the backend's 12 s freshness window is running out.
    // This runs for the whole session, including mid-game with the panel shut.
    const now = Date.now();
    if (changed || now - this.lastPush >= PUSH_INTERVAL_MS) {
      this.busy = true;
      try {
        await setActiveApp(game?.appId ?? "");
        if (!this.started || generation !== this.generation) return;
        if (this.reportFailed) console.info("[legiongo2companion] tdp.foreground_reporting_restored");
        this.reportFailed = false;
        this.lastPush = now;
      } catch (e) {
        if (this.started && generation === this.generation && !this.reportFailed) {
          this.reportFailed = true;
          console.warn("[legiongo2companion] tdp.foreground_reporting_unavailable");
        }
      } finally {
        if (generation === this.generation) this.busy = false;
      }
    }

    if (this.started && generation === this.generation && changed) this.listeners.forEach((fn) => fn(game));
  }
}

// ── Live TDP readings ──────────────────────────────────────────────────────────

// Must stay comfortably under _PANEL_ACTIVE_TTL_S in main.py (90 s).
const PANEL_LEASE_MS = 30000;

/** Live limits and package draw while the page is shown; null until the first reading. */
const useTdpInfo = ({ visible: tabShown = true }: { visible?: boolean } = {}) => {
  const [info, setInfo] = useState<TdpInfo | null>(null);
  const visible = useQuickAccessVisible() && tabShown;

  // Gated on visibility, not just on mount: the page stays mounted while the
  // Quick Access Menu is on another tab, and refreshing it there costs a RAPL
  // read every two seconds for nobody to look at. set_panel_active is what
  // gates the backend loop, so nothing is computed while this is unmounted.
  //
  // The backend pushes each refresh rather than answering a poll: it already
  // recomputed these numbers on exactly this cadence.
  //
  // The lease is renewed rather than set once. The cleanup below drops it, but
  // it never runs if the frontend is torn down outright - a Steam UI restart -
  // and the backend would then keep refreshing forever. Thirty seconds against
  // the backend's ninety leaves room for two lost calls.
  useEffect(() => {
    if (!visible) return;
    let active = true;
    // This advisory lease can expire when Decky disconnects or withdraws the
    // module. A failed renewal/cleanup must not escape as an unhandled promise.
    const updateLease = (value: boolean) => { void setPanelActive(value).catch(() => undefined); };
    updateLease(true);
    const lease = setInterval(() => updateLease(true), PANEL_LEASE_MS);
    const onInfo = (next: TdpInfo) => { if (active) setInfo(next); };
    addEventListener<[TdpInfo]>("tdp_info", onInfo);
    // Seed it: the first push is a full interval away.
    getTdpInfo().then((v) => { if (active) setInfo(v); }).catch(() => undefined);
    return () => {
      active = false;
      clearInterval(lease);
      removeEventListener<[TdpInfo]>("tdp_info", onInfo);
      updateLease(false);
    };
  }, [visible]);
  return info;
};

// ── CPU power controls ────────────────────────────────────────────────────────

const EPP_DEBOUNCE_MS = 350;

const unavailableCpuPowerControls = (error = ""): CpuPowerControls => ({
  success: false,
  available: false,
  cpu_boost: { available: false, enabled: null, error: "" },
  epp: {
    available: false,
    value: null,
    profiles: [],
    numeric_supported: false,
    numeric_value: null,
    min: 0,
    max: 255,
    error: "",
  },
  error,
});

/**
 * The backend ships with this page and canonicalises EPP values itself; fail
 * closed only on a reply without the structure the editor reads, or one for a
 * different profile than the editor asked for.
 */
function checkedCpuPowerControls(value: unknown, appId: string, acProfile: boolean): CpuPowerControls {
  const raw = value as CpuPowerControls | null;
  if (!raw || typeof raw !== "object" || typeof raw.success !== "boolean" || typeof raw.available !== "boolean" ||
      !raw.cpu_boost || typeof raw.cpu_boost !== "object" || !raw.epp || typeof raw.epp !== "object" ||
      !Array.isArray(raw.epp.profiles)) {
    throw new Error("Backend returned incompatible CPU power controls.");
  }
  if (raw.profile && (raw.profile.app_id !== appId || raw.profile.ac_profile !== acProfile)) {
    throw new Error("Backend returned CPU controls for a different profile.");
  }
  return raw;
}

type CpuPowerAction = "boost" | "epp";

interface CpuPowerControlsSectionProps {
  appId?: string;
  acProfile?: boolean;
  expectedAppId?: string;
  powerSource?: boolean;
  onBusyChange?: (owner: object, busy: boolean) => void;
  onCompatibilityNote?: (note: string) => void;
}

const CpuPowerControlsSection: FC<CpuPowerControlsSectionProps> = ({
  appId = "", acProfile = false, expectedAppId = "",
  powerSource = false, onBusyChange, onCompatibilityNote,
}: CpuPowerControlsSectionProps = {}) => {
  const [controls, setControls] = useState<CpuPowerControls | null>(null);
  const [loading, setLoading] = useState(false);
  const [changing, setChanging] = useState<CpuPowerAction | null>(null);
  const [eppPending, setEppPending] = useState(false);
  const [requestError, setRequestError] = useState("");
  const [failedAction, setFailedAction] = useState<CpuPowerAction | null>(null);
  const [eppDraft, setEppDraft] = useState(50);
  const visible = useQuickAccessVisible();

  const mountedRef = useRef(true);
  const visibleRef = useRef(visible);
  const controlsRef = useRef<CpuPowerControls | null>(null);
  const actionRef = useRef<CpuPowerAction | null>(null);
  const actionSequenceRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const readSequenceRef = useRef(0);
  const readInFlightRef = useRef(false);
  const refreshAfterReadRef = useRef(false);
  const refreshControlsRef = useRef<(() => Promise<void>) | null>(null);
  const refreshAfterActionRef = useRef(false);
  const eppPendingRef = useRef(false);
  const flushEppRef = useRef<(() => void) | null>(null);
  const eppTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busyOwnerRef = useRef({});
  const reportBusy = useCallback((busy: boolean) => {
    onBusyChange?.(busyOwnerRef.current, busy);
  }, [onBusyChange]);
  visibleRef.current = visible;

  const acceptControls = useCallback((next: CpuPowerControls) => {
    controlsRef.current = next;
    setControls(next);
    if (!eppPendingRef.current) setEppDraft(eppPercent(editorControls(next).epp, 10));
  }, []);

  const rollbackEppDraft = useCallback(() => {
    const confirmed = controlsRef.current;
    if (confirmed) setEppDraft(eppPercent(editorControls(confirmed).epp, 10));
  }, []);

  const cancelEppDebounce = useCallback(() => {
    if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
    eppTimerRef.current = null;
    flushEppRef.current = null;
    if (!eppPendingRef.current) return;
    eppPendingRef.current = false;
    setEppPending(false);
    if (!actionRef.current) reportBusy(false);
    rollbackEppDraft();
  }, [rollbackEppDraft, reportBusy]);

  const refreshControls = useCallback(async () => {
    if (!mountedRef.current || !visibleRef.current) return;
    const readId = ++readSequenceRef.current;
    // Visibility changes may request a refresh while a mutation is running.
    // Queue it; a getter must never supersede the authoritative setter reply.
    if (actionRef.current) {
      refreshAfterActionRef.current = true;
      return;
    }
    // Reopening or changing scope invalidates the older reply, but needs only
    // one fresh read after it finishes. Never queue overlapping hardware reads.
    if (readInFlightRef.current) {
      refreshAfterReadRef.current = true;
      setLoading(true);
      return;
    }
    readInFlightRef.current = true;
    refreshAfterReadRef.current = false;
    const mutationEpoch = mutationEpochRef.current;
    setLoading(true);
    try {
      const next = checkedCpuPowerControls(await getCpuPowerControls(appId, acProfile), appId, acProfile);
      if (!mountedRef.current || !visibleRef.current ||
          readId !== readSequenceRef.current || mutationEpoch !== mutationEpochRef.current ||
          actionRef.current) {
        if (actionRef.current) refreshAfterActionRef.current = true;
        return;
      }
      acceptControls(next);
      setRequestError(next.success ? "" : next.error);
    } catch (error) {
      if (!mountedRef.current || !visibleRef.current ||
          readId !== readSequenceRef.current || mutationEpoch !== mutationEpochRef.current ||
          actionRef.current) return;
      console.warn("[legiongo2companion] tdp.cpu_controls_unavailable");
      const message = "CPU power controls are unavailable in this backend build.";
      acceptControls(unavailableCpuPowerControls(message));
      setRequestError(message);
    } finally {
      readInFlightRef.current = false;
      if (mountedRef.current && readId === readSequenceRef.current &&
          mutationEpoch === mutationEpochRef.current && !actionRef.current) {
        setLoading(false);
      }
      if (refreshAfterReadRef.current) {
        refreshAfterReadRef.current = false;
        if (mountedRef.current && visibleRef.current) void refreshControlsRef.current?.();
      }
    }
  }, [acceptControls, appId, acProfile]);
  refreshControlsRef.current = refreshControls;

  useEffect(() => {
    if (!visible) {
      flushEppRef.current?.();
      ++readSequenceRef.current;
      setLoading(false);
      return;
    }
    if (actionRef.current) {
      refreshAfterActionRef.current = true;
      return;
    }
    void refreshControls();
  }, [visible, powerSource, cancelEppDebounce, refreshControls]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      flushEppRef.current?.();
      mountedRef.current = false;
      visibleRef.current = false;
      ++readSequenceRef.current;
      ++actionSequenceRef.current;
      ++mutationEpochRef.current;
      if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
      eppTimerRef.current = null;
      eppPendingRef.current = false;
      if (!actionRef.current) reportBusy(false);
    };
  }, []);

  const runAction = async (
    kind: CpuPowerAction,
    request: () => Promise<CpuPowerControls>,
  ) => {
    if (!mountedRef.current || actionRef.current) return;
    const actionId = ++actionSequenceRef.current;
    ++mutationEpochRef.current;
    ++readSequenceRef.current;
    actionRef.current = kind;
    reportBusy(true);
    setLoading(false);
    setChanging(kind);
    setRequestError("");
    setFailedAction(null);
    let refreshNeeded = false;

    try {
      const next = checkedCpuPowerControls(await request(), appId, acProfile);
      if (!mountedRef.current) {
        if (!next.success) notify("TDP", next.error || next.epp.error || next.cpu_boost.error);
        return;
      }
      if (actionId !== actionSequenceRef.current) {
        refreshNeeded = true;
        return;
      }
      acceptControls(next);
      if (!next.success) {
        const controlError = kind === "boost" ? next.cpu_boost.error : next.epp.error;
        const message = next.error || controlError || "The backend rejected the change.";
        setRequestError(message);
        setFailedAction(kind);
        notify("TDP", message);
      }
    } catch (error) {
      if (!mountedRef.current) {
        notifyFailure("TDP", error);
        return;
      }
      if (actionId !== actionSequenceRef.current) {
        refreshNeeded = true;
        return;
      }
      refreshNeeded = true;
      if (kind === "epp") rollbackEppDraft();
      setRequestError(errorText(error));
      setFailedAction(kind);
      notifyFailure("TDP", error);
    } finally {
      reportBusy(false);
      const current = actionId === actionSequenceRef.current;
      if (current) {
        actionRef.current = null;
        if (mountedRef.current) setChanging(null);
      }
      if (!mountedRef.current) return;

      const shouldRefresh = refreshNeeded || refreshAfterActionRef.current || !current;
      if (current) refreshAfterActionRef.current = false;
      if (shouldRefresh) {
        if (actionRef.current) refreshAfterActionRef.current = true;
        else if (visibleRef.current) void refreshControls();
      }
    }
  };

  const changeBoost = (enabled: boolean) => {
    const boost = controlsRef.current && editorControls(controlsRef.current).cpu_boost;
    if (!visibleRef.current || !boost?.available || boost.enabled == null ||
        actionRef.current || eppPendingRef.current || enabled === boost.enabled) return;
    void runAction("boost", () => setCpuBoost(enabled, appId, acProfile, expectedAppId));
  };

  const changeEpp = (value: string) => {
    const current = controlsRef.current && editorControls(controlsRef.current);
    const epp = current?.epp;
    if (!visibleRef.current || !epp?.available || actionRef.current ||
        (value === epp.value && (current?.profile?.epp == null || value === current.profile.epp))) return;
    void runAction("epp", () => setEpp(value, appId, acProfile, expectedAppId));
  };

  const scheduleNumericEpp = (value: number) => {
    const epp = controlsRef.current && editorControls(controlsRef.current).epp;
    if (!mountedRef.current || !visibleRef.current || !epp?.available ||
        !epp.numeric_supported || actionRef.current) return;
    const percent = Math.round(Math.min(Math.max(value, 0), 100) / 10) * 10;
    const rawValue = eppValue(percent);
    setEppDraft(percent);
    eppPendingRef.current = true;
    setEppPending(true);
    reportBusy(true);
    if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
    const flush = () => {
      // clearTimeout cannot retract a callback already queued by the browser.
      // Only the current, unflushed gesture may clear the timer or write EPP.
      if (flushEppRef.current !== flush) return;
      if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
      eppTimerRef.current = null;
      flushEppRef.current = null;
      eppPendingRef.current = false;
      setEppPending(false);
      // Keep the scope and foreground game from this gesture, including when
      // a remount flushes the slider after switching game or battery/AC editor.
      void runAction("epp", () => setEpp(rawValue, appId, acProfile, expectedAppId));
    };
    flushEppRef.current = flush;
    eppTimerRef.current = setTimeout(flush, EPP_DEBOUNCE_MS);
  };

  // The compatibility note is a technical fact; the page shows it under Show details.
  useEffect(() => { onCompatibilityNote?.(controls?.epp.compatibility_note ?? ""); },
    [controls?.epp.compatibility_note, onCompatibilityNote]);

  if (!controls) return (
    <PanelSection title="CPU">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  const shown = editorControls(controls);
  const boost = shown.cpu_boost;
  const epp = shown.epp;
  const busy = loading || changing !== null || eppPending;
  const title = eppTitle(epp);
  const biasProfiles = epp.profiles.filter((profile) =>
    Object.prototype.hasOwnProperty.call(EPP_NAMED_VALUES, profile));
  const specialProfiles = epp.profiles.filter((profile) =>
    !Object.prototype.hasOwnProperty.call(EPP_NAMED_VALUES, profile));
  const profileIndex = epp.value ? biasProfiles.indexOf(epp.value) : -1;
  const profileOffset = profileIndex >= 0 ? 0 : 1;
  const discreteProfileValue = profileIndex >= 0 ? profileIndex : 0;
  const noDiscreteChoice = profileIndex >= 0
    ? biasProfiles.length < 2
    : biasProfiles.length < 1;
  // A failed change shows on its control; a failed read on CPU Boost, the first row.
  const globalError = requestError || controls.error;
  const errorFor = (kind: CpuPowerAction) =>
    (failedAction ?? "boost") === kind ? globalError : "";

  return (
    <PanelSection title="CPU">
      <PanelSectionRow>
        <ToggleField
          label="CPU Boost"
          description={errorFor("boost") || (
            boostUsable(boost) && changing === "boost" ? "Applying…" : boostNote(boost))}
          checked={boost.enabled === true}
          disabled={busy || !boostUsable(boost)}
          onChange={changeBoost}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        {epp.numeric_supported ? (
          <SliderField
            label={title}
            value={eppDraft}
            min={0}
            max={100}
            step={10}
            showValue={true}
            valueSuffix="%"
            disabled={loading || changing !== null || !epp.available}
            onChange={scheduleNumericEpp}
            description={errorFor("epp") || (
              epp.available && (changing === "epp" || eppPending) ? "Applying…" : eppNote(epp))}
          />
        ) : (
          <SliderField
            label={title}
            value={discreteProfileValue}
            min={0}
            max={Math.max(1, biasProfiles.length - 1 + profileOffset)}
            step={1}
            showValue={false}
            disabled={busy || !epp.available || noDiscreteChoice}
            onChange={(index) => {
              const profile = biasProfiles[Math.round(index) - profileOffset];
              if (profile) changeEpp(profile);
            }}
            description={errorFor("epp") || (
              !epp.available ? eppUnavailable(epp)
              : changing === "epp" ? "Applying…"
              : noDiscreteChoice ? "No other choice on this device."
              : profileIndex < 0 ? `Choose: ${biasProfiles.map(eppLabel).join(" / ")}`
              : EPP_PREFERENCE)}
          />
        )}
      </PanelSectionRow>
      {!epp.numeric_supported && specialProfiles.map((profile) => (
        <PanelSectionRow key={`epp-${profile}`}>
          <ButtonItem
            layout="below"
            disabled={busy || !epp.available || (controls.profile?.epp ?? epp.value) === profile}
            onClick={() => changeEpp(profile)}
          >
            {`Use ${eppLabel(profile)}`}
          </ButtonItem>
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
};

// ── Main content ───────────────────────────────────────────────────────────────
type Scope = "global" | "game" | "game_ac";

/** State, effects and shared operations of the TDP page; the sections below render from them. */
const useTdpState = (tabShown: boolean) => {
  const [ready,    setReady]    = useState(false);
  const [setupErr, setSetupErr] = useState<string | null>(null);
  const [initializing, setInitializing] = useState(false);

  const [tuning,   setTuning]   = useState<Tuning>(fromAbsolute(16, 25, 30));
  const [acTuning, setAcTuning] = useState<Tuning>(fromAbsolute(16, 25, 30));
  const [preset,   setPreset]   = useState<PresetKey>("balanced");

  const [stdCaps, setStdCaps] = useState<Caps>(FALLBACK_STD);
  const [maxCaps, setMaxCaps] = useState<Caps>(FALLBACK_MAX);
  // Hardware with no ryzenadj path has nothing above the firmware to unlock.
  const [extrasAvailable, setExtrasAvailable] = useState(true);
  const [tdpAvailable, setTdpAvailable] = useState(true);
  const [tdpError, setTdpError] = useState("");
  const [helperRetryAvailable, setHelperRetryAvailable] = useState(false);
  // Presets come from the backend alongside the hardware ceilings.
  const [presets, setPresets] = useState<PresetTable>(PRESETS);
  const [batteryPresets, setBatteryPresets] = useState<PresetTable | undefined>(undefined);
  const [minW,    setMinW]    = useState(FALLBACK_MIN);

  const [enabled,       setEnabled]       = useState(true);
  const [game,          setGame]          = useState<RunningGame | null>(null);
  // The running game uses its own profile; an unused one may still exist (profileExists).
  const [perGame,       setPerGame]       = useState(false);
  const [profileExists, setProfileExists] = useState(false);

  const [acOnline,      setAcOnline]      = useState(false);
  const [acSeparate,    setAcSeparate]    = useState(false);
  const [editingAc,     setEditingAc]     = useState(false);

  const [globalProfile, setGlobalProfile] = useState<{ spl: number; sppt: number; fppt: number; preset: string | undefined }>({ spl: 16, sppt: 25, fppt: 30, preset: undefined });
  const [extrasUnlocked, setExtrasUnlocked] = useState(false);
  const [advancedTdpControl, setAdvancedTdpControl] = useState(true);
  const [separateBattery, setSeparateBattery] = useState(false);

  const [savedPreset,   setSavedPreset]   = useState<string | undefined>(undefined);
  const [savedAcPreset, setSavedAcPreset] = useState<string | undefined>(undefined);

  const [operationLoading, setLoading] = useState(false);
  const [profilePending, setProfilePending] = useState(true);
  const [profileError, setProfileError] = useState("");
  const [profileAttempt, setProfileAttempt] = useState(0);
  const loading = operationLoading || profilePending || !!profileError;
  const [cpuBusy, setCpuBusy] = useState(false);
  // Custom sliders are a draft until Apply; the base restores what Discard drops.
  const [dirty, setDirty] = useState(false);
  const dirtyRef = useRef(false);
  const draftBaseRef = useRef<{ ac: boolean; tuning: Tuning } | null>(null);
  // The failed control shows its error until the next successful action.
  const [failure, setFailure] = useState<{ control: string; text: string } | null>(null);
  const [details, setDetails] = useState(false);
  const [cpuNote, setCpuNote] = useState("");

  const visible = useQuickAccessVisible();
  const info = useTdpInfo({ visible: tabShown && ready && enabled });

  const noGameSyncedRef = useRef(false);
  const initialPowerReadRef = useRef(false);
  const powerSourceRevisionRef = useRef(0);
  const profileRequestRef = useRef(0);
  const cpuBusyOwnersRef = useRef(new Set<object>());
  const pageMountedRef = useRef(true);
  const visibleRef = useRef(visible);
  visibleRef.current = visible;
  const readyRef = useRef(false);
  const initReadRef = useRef<Promise<void> | null>(null);
  const initRevisionRef = useRef(0);
  const capsReadRef = useRef<Promise<CapsInfo> | null>(null);
  const operationRef = useRef(0);
  const operationBusyRef = useRef(false);
  const scopeRef = useRef({ appId: AppWatcher.currentGame()?.appId ?? "", revision: 0 });
  const adoptedGameRef = useRef(scopeRef.current.appId);
  const globalProfileRef = useRef(globalProfile);
  globalProfileRef.current = globalProfile;
  const profileReadyRef = useRef<{ appId: string; revision: number } | null>(null);
  const captureScope = () => {
    const appId = AppWatcher.currentGame()?.appId ?? "";
    if (appId !== scopeRef.current.appId)
      scopeRef.current = { appId, revision: scopeRef.current.revision + 1 };
    return { ...scopeRef.current };
  };
  const scopeCurrent = (scope: { appId: string; revision: number }) => {
    const current = captureScope();
    return pageMountedRef.current && current.appId === scope.appId && current.revision === scope.revision;
  };
  const renderScope = captureScope();
  const beginOperation = (requiresTdp = true, requiresProfile = true) => {
    if (!scopeCurrent(renderScope)) return null;
    if (requiresProfile && (!profileReadyRef.current || !scopeCurrent(profileReadyRef.current))) return null;
    if (operationBusyRef.current || (requiresTdp && (cpuBusyOwnersRef.current.size > 0 || !tdpAvailable))) return null;
    const scope = { ...captureScope(), operation: ++operationRef.current };
    operationBusyRef.current = true;
    if (requiresTdp) profileRequestRef.current += 1;
    setLoading(true);
    return scope;
  };
  const operationCurrent = (scope: { appId: string; revision: number; operation: number }) =>
    scope.operation === operationRef.current && scopeCurrent(scope);
  const finishOperation = (scope: { operation: number }) => {
    if (scope.operation !== operationRef.current) return;
    operationBusyRef.current = false;
    if (pageMountedRef.current) setLoading(false);
  };
  const readCaps = () => {
    if (capsReadRef.current) return capsReadRef.current;
    const request = getCaps();
    capsReadRef.current = request;
    const done = () => { if (capsReadRef.current === request) capsReadRef.current = null; };
    request.then(done, done);
    return request;
  };
  const adoptCaps = (c: CapsInfo) => {
    if (c?.std && c?.max) {
      setStdCaps(c.std); setMaxCaps(c.max); setMinW(c.min);
      setPresets(c.presets ?? PRESETS);
      setBatteryPresets(c.battery_presets);
    }
    const state = capsState(c);
    setExtrasAvailable(state.extrasAvailable);
    setTdpAvailable(state.tdpAvailable);
    setTdpError(state.tdpError);
    setHelperRetryAvailable(state.helperRetryAvailable);
  };
  const onCpuBusyChange = useCallback((owner: object, busy: boolean) => {
    if (busy) cpuBusyOwnersRef.current.add(owner);
    else cpuBusyOwnersRef.current.delete(owner);
    if (pageMountedRef.current) setCpuBusy(cpuBusyOwnersRef.current.size > 0);
  }, []);

  useEffect(() => {
    pageMountedRef.current = true;
    return () => { pageMountedRef.current = false; initRevisionRef.current += 1; };
  }, []);

  // A preset still settling when the page closes is dropped, like a draft.

  const caps    = extrasUnlocked && extrasAvailable ? maxCaps : stdCaps;
  const customCaps = advancedTdpControl ? caps : simpleCaps(caps, extrasUnlocked && extrasAvailable);
  const active  = editingAc ? acTuning : tuning;
  const setActive = editingAc ? setAcTuning : setTuning;

  const clearDraft = () => { dirtyRef.current = false; draftBaseRef.current = null; setDirty(false); };
  const done = (body: string) => {
    if (!pageMountedRef.current) return;
    setFailure(null);
    notify("TDP", body);
  };
  /** A toast, plus the error on the control that failed. */
  const showError = (control: string, e: unknown) => {
    notifyFailure("TDP", e);
    if (pageMountedRef.current) setFailure({ control, text: errorText(e) });
  };
  const failureFor = (control: string) => failure?.control === control ? failure.text : undefined;

  /** Puts a saved game profile in the editor; false when it is missing or corrupt. */
  const showGameProfile = (gp: GameProfile) => {
    if (!gp.exists || !gp.profile) return false;
    const p  = gp.profile;
    const ac = gp.ac_profile ?? { spl: p.spl, sppt: p.sppt, fppt: p.fppt, ac_preset: "" };
    const storedPreset = (p.preset as PresetKey | undefined) || undefined;
    const battery = savedTuning(p, storedPreset, presets, advancedTdpControl, customCaps, minW);
    clearDraft();
    setPerGame(true);
    setProfileExists(true);
    setTuning(battery.tuning);
    setAcTuning(savedTuning(ac, ac.ac_preset, presets, advancedTdpControl, customCaps, minW).tuning);
    setAcSeparate(gp.ac_separate);
    // The editor starts on the source the console runs on now.
    setEditingAc(gp.ac_separate && acOnline);
    setSavedPreset(storedPreset);
    setSavedAcPreset(gp.ac_separate ? (ac.ac_preset ?? "") : undefined);
    setPreset(battery.preset);
    return true;
  };

  /** Show Global settings after the game stopped using (or lost) its own profile. */
  const adoptGlobalSettings = (s: Settings) => {
    const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
    clearDraft();
    setTuning(fromAbsolute(w, sw, fw));
    setPreset(resolvedPreset(w, sw, fw, s.active_preset, presets));
    setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
  };

  // ── Init ─────────────────────────────────────────────────────────────────────
  const initialize = async () => {
    if (!pageMountedRef.current || !visibleRef.current || readyRef.current || initReadRef.current) return;
    const revision = initRevisionRef.current, scope = captureScope();
    // Finish the initial snapshot already admitted while visible. A later
    // opening still refreshes live power state; only retries stop while hidden.
    const current = () => pageMountedRef.current &&
      revision === initRevisionRef.current && scopeCurrent(scope);
    setInitializing(true);
    const request = (async () => {
      try {
        const r = await isReady();
        if (!current()) return;
        if (r.error) throw new Error(r.error);
        if (r.ready) {
          const powerRevision = powerSourceRevisionRef.current;
          const reads = [getSettings(), getPowerSource(), getExtrasUnlocked(), readCaps()] as const;
          let snapshot: [Settings, PowerSource, boolean, CapsInfo];
          try { snapshot = await Promise.all(reads); }
          catch (error) {
            if (current()) setSetupErr(errorText(error));
            // One rejected read must not start duplicates of the other reads
            // that are still running in this initialization attempt.
            await Promise.allSettled(reads);
            throw error;
          }
          const [s, ps, eu, c] = snapshot;
          if (!current()) return;
          const machinePresets = c?.presets ?? PRESETS;
          adoptCaps(c);
          // Fresh caps, not page state; a legacy reply may lack std/max, so read them only for simple Custom.
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          const selected = resolvedPreset(w, sw, fw, s.active_preset, machinePresets);
          const unlocked = eu && c.extras !== false;
          const view = s.advanced_tdp_control === false && selected === "custom"
            ? simpleTuning(w, simpleCaps(unlocked ? c.max : c.std, unlocked), c.min)
            : fromAbsolute(w, sw, fw);
          setTuning(view);
          setGlobalProfile({ ...absolute(view), preset: s.active_preset || undefined });
          setPreset(selected);
          setEnabled(s.enabled !== false);
          setAdvancedTdpControl(s.advanced_tdp_control !== false);
          setSeparateBattery(s.separate_battery_presets === true);
          if (powerRevision === powerSourceRevisionRef.current) setAcOnline(ps.ac);
          initialPowerReadRef.current = true;
          noGameSyncedRef.current = AppWatcher.currentGame() === null;
          setExtrasUnlocked(eu);
          setSetupErr(null);
          readyRef.current = true;
          setReady(true);
        } else {
          setSetupErr("TDP and CPU controls are still starting. Retry status if this continues.");
        }
      } catch (error) {
        if (current()) setSetupErr(errorText(error));
      }
    })();
    initReadRef.current = request;
    try { await request; }
    finally {
      if (initReadRef.current === request) initReadRef.current = null;
      if (current()) setInitializing(false);
      else if (pageMountedRef.current && visibleRef.current && !readyRef.current) void initialize();
    }
  };
  useEffect(() => {
    if (!visible || readyRef.current) return;
    let active = true, attempts = 0;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      if (!active || !visibleRef.current || readyRef.current) return;
      attempts += 1;
      await initialize();
      if (active && visibleRef.current && !readyRef.current && attempts < 3)
        retryTimer = setTimeout(check, 1000);
    };
    void check();
    return () => { active = false;
      if (retryTimer !== undefined) clearTimeout(retryTimer); };
  }, [visible]);

  useEffect(() => {
    if (!ready || !visible || extrasAvailable || !helperRetryAvailable) return;
    let active = true;
    const check = async () => {
      const scope = captureScope();
      try {
        const c = await readCaps();
        if (active && visibleRef.current && scopeCurrent(scope)) adoptCaps(c);
      } catch { /* Keep the confirmed availability and the explicit retry. */ }
    };
    const timer = setInterval(check, 5000);
    return () => { active = false; clearInterval(timer); };
  }, [ready, visible, extrasAvailable, helperRetryAvailable]);

  // The Desktop app may change Separate battery presets meanwhile: re-read it on every
  // later opening (initialization has just read it for the first one) and every ten seconds
  // while the page is open.
  const batteryReadRef = useRef(false);
  useEffect(() => {
    if (!ready || !visible) return;
    let active = true, reading = false;
    const read = () => {
      if (reading) return;
      reading = true;
      const operation = operationRef.current;
      getSettings().then((s) => {
        // A toggle from this page since the read has the newer answer.
        if (active && !operationBusyRef.current && operation === operationRef.current)
          setSeparateBattery(s.separate_battery_presets === true);
      }).catch(() => undefined).finally(() => { reading = false; });
    };
    if (batteryReadRef.current) read(); else batteryReadRef.current = true;
    const timer = setInterval(read, 10000);
    return () => { active = false; clearInterval(timer); };
  }, [ready, visible]);

  // ── Game detection ────────────────────────────────────────────────────────────
  // AppWatcher owns this and runs for the whole session, so the backend keeps
  // getting the authoritative appid while the panel is closed. Here we only
  // adopt what it reports.
  useEffect(() => {
    const adoptGame = (next: RunningGame | null) => {
      const appId = next?.appId ?? "";
      const changed = adoptedGameRef.current !== appId;
      adoptedGameRef.current = appId;
      captureScope();
      if (changed) {
        profileReadyRef.current = null;
        setProfilePending(true);
        setProfileError("");
        setPerGame(false);
        setProfileExists(false);
        setAcSeparate(false);
        setEditingAc(false);
        setSavedPreset(undefined);
        setSavedAcPreset(undefined);
        const global = globalProfileRef.current;
        const view = fromAbsolute(global.spl, global.sppt, global.fppt);
        setTuning(view);
        setAcTuning(view);
        setPreset(resolvedPreset(global.spl, global.sppt, global.fppt, global.preset, presets));
        clearDraft();
        operationRef.current += 1;
        operationBusyRef.current = false;
        profileRequestRef.current += 1;
        setLoading(false);
        setFailure(null);
      }
      setGame(next);
    };
    adoptGame(AppWatcher.currentGame());
    return AppWatcher.listen(adoptGame);
  }, []);

  // ── AC state ──────────────────────────────────────────────────────────────────
  // The enforce loop already reads the charger every five seconds to decide
  // which profile applies, and emits when the answer changes - so the panel
  // subscribes instead of polling. Keep events during init and hidden time so a
  // slow initial read cannot replace a newer event.
  useEffect(() => {
    const onPower = (ps: PowerSource) => {
      powerSourceRevisionRef.current += 1;
      setAcOnline(ps.ac);
    };
    addEventListener<[PowerSource]>("power_source", onPower);
    return () => removeEventListener<[PowerSource]>("power_source", onPower);
  }, []);

  useEffect(() => {
    if (!ready) return;
    const initialRead = initialPowerReadRef.current;
    initialPowerReadRef.current = false;
    if (!visible) return;
    let active = true;
    // Init already supplied this value; later openings still seed from hardware.
    if (!initialRead) {
      const revision = powerSourceRevisionRef.current, scope = captureScope();
      getPowerSource().then((ps) => {
        if (active && scopeCurrent(scope) && revision === powerSourceRevisionRef.current) setAcOnline(ps.ac);
      }).catch(() => undefined);
    }
    return () => { active = false; };
  }, [ready, visible]);

  // ── Show the game's profile when game / ready / enabled changes ─────────────
  // The backend applies it: AppWatcher awaits set_active_app, which runs an
  // enforce pass, before it notifies this page, so the panel only reads here.
  useEffect(() => {
    if (!ready) return;
    const scope = captureScope();
    const request = ++profileRequestRef.current;
    profileReadyRef.current = null;
    setProfilePending(true);
    setProfileError("");
    const current = (expected: string) =>
      scopeCurrent(scope) && request === profileRequestRef.current &&
      (AppWatcher.currentGame()?.appId ?? "") === expected;

    const adoptGlobal = async (expected: string) => {
      const s = await getSettings();
      if (!current(expected)) return;
      const { preset: selected, tuning: view } = savedTuning(s, s.active_preset, presets, advancedTdpControl, customCaps, minW);
      clearDraft();
      setPerGame(false);
      setAcSeparate(false);
      setEditingAc(false);
      setSavedPreset(undefined);
      setSavedAcPreset(undefined);
      setTuning(view);
      setAcTuning(view);
      setPreset(selected);
      setGlobalProfile({ ...absolute(view), preset: s.active_preset || undefined });
    };
    const complete = (expected: string) => {
      if (!current(expected)) return;
      profileReadyRef.current = scope;
      noGameSyncedRef.current = expected === "";
      setProfilePending(false);
    };
    const failed = (expected: string, error: unknown) => {
      if (!current(expected)) return;
      setProfilePending(false);
      setProfileError(errorText(error));
      notifyFailure("TDP", error);
    };

    if (!game || !enabled) {
      const expected = game?.appId ?? "";
      if (perGame) setPerGame(false);
      if (!game) setProfileExists(false);
      // Initialization already supplies a complete no-game snapshot.
      if (!game && noGameSyncedRef.current) {
        complete(expected);
        return;
      }
      setSavedPreset(undefined);
      setSavedAcPreset(undefined);
      setAcSeparate(false);
      setEditingAc(false);
      (async () => {
        try {
          await adoptGlobal(expected);
          complete(expected);
        } catch (e: unknown) {
          if (current(expected)) noGameSyncedRef.current = false;
          failed(expected, e);
        }
      })();
      return () => { profileRequestRef.current += 1; };
    }

    noGameSyncedRef.current = false;

    (async () => {
      try {
        const requestedGame = game;
        const gp = await getGameProfile(requestedGame.appId);
        if (!current(requestedGame.appId)) return;
        if (gp.error && !gp.exists) throw new Error(gp.error);
        if (gp.exists && !gp.profile) throw new Error("Game profile data is missing or corrupt.");
        // An unused profile is kept for later; the game runs on Global settings.
        if (gp.exists && gp.active !== false) showGameProfile(gp);
        else {
          await adoptGlobal(requestedGame.appId);
          if (current(requestedGame.appId)) setProfileExists(gp.exists);
        }
        complete(requestedGame.appId);
      } catch (e: unknown) {
        failed(game.appId, e);
      }
    })();
    return () => { profileRequestRef.current += 1; };
  }, [game?.appId, ready, enabled, tdpAvailable, profileAttempt]);

  const capsNow = { tdpAvailable, extrasAvailable, helperRetryAvailable, tdpError };
  return { ready, setupErr, initializing, initialize, tuning, setTuning, acTuning, setAcTuning, preset, setPreset,
    stdCaps, extrasAvailable, tdpAvailable, presets, minW, enabled, setEnabled, game, perGame, setPerGame,
    profileExists, setProfileExists, acOnline, acSeparate, setAcSeparate, editingAc, setEditingAc, globalProfile,
    setGlobalProfile, extrasUnlocked, setExtrasUnlocked, advancedTdpControl, setAdvancedTdpControl, savedPreset,
    separateBattery, setSeparateBattery, batteryPresets, setSavedPreset, savedAcPreset, setSavedAcPreset, operationLoading, profilePending, setProfilePending,
    profileError, setProfileError, setProfileAttempt, loading, cpuBusy, setDirty, dirty, dirtyRef, draftBaseRef,
    setFailure, details, setDetails, cpuNote, setCpuNote, info, cpuBusyOwnersRef, onCpuBusyChange, beginOperation,
    operationCurrent, finishOperation, readCaps, adoptCaps, caps, customCaps, active, setActive, clearDraft, done,
    showError, failureFor, showGameProfile, adoptGlobalSettings, capsNow };
};
type TdpState = ReturnType<typeof useTdpState>;

// Sections are plain render functions without hooks, called inline by TdpPage:
// the page keeps one element tree and one hook order.

const noticeSection = (s: TdpState) => {
  const { capsNow, extrasUnlocked, profileError, profilePending, setProfilePending, setProfileError,
    setProfileAttempt } = s;
  const notice = tdpNotice(capsNow, extrasUnlocked);
  return (notice || profileError) && <PanelSection>
    {notice && infoRow(notice[0], notice[1])}
    {profileError && <>
      {infoRow("Could not load profile", profileError)}
      <PanelSectionRow><ButtonItem layout="below" onClick={() => {
        if (profilePending) return;
        setProfilePending(true); setProfileError("");
        setProfileAttempt(attempt => attempt + 1);
      }}>Retry profile</ButtonItem></PanelSectionRow>
    </>}
  </PanelSection>;
};

const powerSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, dirtyRef,
    draftBaseRef, preset, setPreset, tuning, setTuning, acTuning, setAcTuning, active, setActive,
    advancedTdpControl, caps, customCaps, minW, presets, editingAc, setEditingAc, perGame, setPerGame, game,
    acSeparate, setAcSeparate, acOnline, tdpAvailable, enabled, setEnabled, setSavedPreset, savedPreset,
    setSavedAcPreset, savedAcPreset, globalProfile, setGlobalProfile, adoptGlobalSettings, showGameProfile,
    setProfileAttempt, profilePending, profileError, info, extrasUnlocked, stdCaps, cpuBusy, loading,
    cpuBusyOwnersRef, separateBattery, batteryPresets } = s;
  /** Run `proceed` now, or after confirming that the unsaved Custom limits are dropped. */
  const askDiscard = (proceed: () => void) => {
    if (!dirtyRef.current) return proceed();
    confirmAction("Discard unsaved changes?", "Your changes to these limits haven't been applied.", "Discard", () => {
      const base = draftBaseRef.current;
      if (base) (base.ac ? setAcTuning : setTuning)(base.tuning);
      clearDraft();
      proceed();
    });
  };

  // ── Preset handler ────────────────────────────────────────────────────────────
  const handlePresetChange = async (key: PresetKey) => {
    const scope = beginOperation();
    if (!scope) return;
    const prevPreset = preset;
    const prevTuning = tuning, prevAcTuning = acTuning;
    // A preset replaces the Custom sliders, so their draft is dropped without asking.
    clearDraft();
    setPreset(key);
    if (key === "custom") {
      if (!advancedTdpControl) setActive(simpleTuning(active.spl, customCaps, minW));
      finishOperation(scope);
      return;
    }

    const vals = presets[key];
    const next = normalise(fromAbsolute(vals.spl, vals.sppt, vals.fppt), caps, minW);
    if (editingAc) setAcTuning(next); else setTuning(next);
    const appId = (perGame && game) ? game.appId : "";
    const a = absolute(next);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, key);
        if (!operationCurrent(scope)) return;
        if (!r.success) throw new Error(r.stderr || "The preset was not saved.");
        setSavedAcPreset(key);
        done(`${PRESET_LABELS[key]} saved for ${game!.name} on charger.`);
      } else {
        const r = await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, key,
          scope.appId);
        if (!operationCurrent(scope)) return;
        if (!r.success) throw new Error(r.stderr || "The preset was not applied.");
        if (!appId) setGlobalProfile({ ...a, preset: key });
        else setSavedPreset(key);
        done(appId ? `${PRESET_LABELS[key]} saved for ${game!.name}.` : `${PRESET_LABELS[key]} applied.`);
      }
    } catch (e: unknown) {
      if (!operationCurrent(scope)) return;
      setPreset(prevPreset);
      if (editingAc) setAcTuning(prevAcTuning); else setTuning(prevTuning);
      showError("preset", e);
    } finally { finishOperation(scope); }
  };
  // ── Settings for {game} ───────────────────────────────────────────────────────
  const scope: Scope = !perGame ? "global" : acSeparate ? "game_ac" : "game";
  const applyScope = async (next: Scope) => {
    if (!game) return;
    const op = beginOperation(tdpAvailable);
    if (!op) return;
    const app = game;
    try {
      // Without a TDP provider the choice only selects which CPU settings to edit.
      if (!tdpAvailable) { setPerGame(next !== "global"); return; }
      if (next === "global") {
        const result = await setGameProfileActive(app.appId, false);
        if (!operationCurrent(op)) return;
        if (!result.success) throw new Error(result.stderr || "Could not switch to Global settings.");
        setPerGame(false); setAcSeparate(false); setEditingAc(false);
        setSavedPreset(undefined); setSavedAcPreset(undefined);
        const s = await getSettings();
        if (!operationCurrent(op)) return;
        adoptGlobalSettings(s);
        done(`${app.name} uses Global settings.`);
        return;
      }
      let profile: GameProfile | null = null;
      if (!perGame) {
        const result = await setGameProfileActive(app.appId, true);
        if (!operationCurrent(op)) return;
        if (!result.success) throw new Error(result.stderr || "Could not turn on this game's settings.");
        profile = await getGameProfile(app.appId);
        if (!operationCurrent(op)) return;
        if (!profile.exists || !profile.profile) throw new Error("Game profile data is missing or corrupt.");
        showGameProfile(profile);
      }
      const separate = profile ? profile.ac_separate : acSeparate;
      if ((next === "game_ac") !== separate) {
        // Hand the stored charger values back unchanged, so splitting again restores them.
        const stored = profile ?? await getGameProfile(app.appId);
        if (!operationCurrent(op)) return;
        const ac = stored.ac_profile;
        if (!stored.exists || !ac) throw new Error("Game profile data is missing or corrupt.");
        const result = await setGameAcProfile(app.appId, ac.spl, ac.sppt, ac.fppt, next === "game_ac", "");
        if (!operationCurrent(op)) return;
        if (!result.success) throw new Error(result.stderr || "Could not change the charger settings.");
        setAcSeparate(next === "game_ac");
        setAcTuning(savedTuning(ac, ac.ac_preset, presets, advancedTdpControl, customCaps, minW).tuning);
        setEditingAc(next === "game_ac" && acOnline);
        setSavedAcPreset(next === "game_ac" ? (ac.ac_preset ?? "") : undefined);
      }
      done(next === "game_ac" ? `${app.name} has separate battery and charger settings.`
        : `Only ${app.name} uses these settings.`);
    } catch (e: unknown) {
      if (!operationCurrent(op)) return;
      showError("scope", e);
      // A step may have landed before the failure; show what the backend kept.
      setProfileAttempt(attempt => attempt + 1);
    } finally { finishOperation(op); }
  };

  // ── Enable / disable plugin ───────────────────────────────────────────────────
  const handleEnabledToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
      const result = await setPluginEnabled(checked);
      if (!operationCurrent(op)) return;
      if (!result.success) throw new Error(result.stderr || "Could not change TDP control.");
      setEnabled(checked);
      done(checked ? "TDP control on." : "TDP control off. Lenovo's limits are back.");
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("enabled", e);
    } finally { finishOperation(op); }
  };

  // What runs now: the game's charger values on AC when it has them, else its battery or the global values.
  const running = perGame && acSeparate && acOnline ? acTuning : tuning;
  const runningStored = !perGame ? globalProfile.preset : acSeparate && acOnline ? savedAcPreset : savedPreset;
  const stored = absolute(running);
  const nowKey = resolvedPreset(stored.spl, stored.sppt, stored.fppt, runningStored, presets);
  const nowLabel = PRESET_LABELS[nowKey];
  // A preset runs its battery values on battery with Separate battery presets; Custom runs as saved.
  const now = nowKey === "custom" ? stored : shownPresets(presets, batteryPresets, separateBattery, !acOnline)[nowKey];
  const enableDescription = failureFor("enabled") ?? (enabled && (profilePending || profileError) ? "Reading profile…"
    : powerSummary(enabled, perGame && game ? game.name : "Global", nowLabel, now,
      info?.success ? info.values?.package_draw : null, extrasUnlocked, stdCaps));
  const presetDescription = failureFor("preset") ?? presetNote(preset, shownPresets(presets, batteryPresets,
    separateBattery, !editingAc && ((perGame && acSeparate) || !acOnline)));
  const scopeDescription = !game ? "" : failureFor("scope") ?? scopeNote(scope === "game_ac" ? "charger" : scope, game.name);
  const sourceDescription = sourceNote(acOnline, editingAc);

  return (
    <PanelSection title="Power">
      <PanelSectionRow>
        <ToggleField
          label="Enable TDP control"
          description={enableDescription}
          checked={enabled}
          disabled={cpuBusy || loading || !tdpAvailable}
          onChange={handleEnabledToggle}
        />
      </PanelSectionRow>
      {enabled && game && <PanelSectionRow>
        <DropdownItem
          label={`Settings for ${game.name}`}
          description={scopeDescription}
          selectedOption={scope}
          rgOptions={[{ data: "global", label: "Global" }, { data: "game", label: "This game" },
            { data: "game_ac", label: "This game + charger" }]}
          disabled={cpuBusy || loading}
          onChange={option => { if (option.data !== scope && !cpuBusyOwnersRef.current.size) askDiscard(() => void applyScope(option.data)); }}
        />
      </PanelSectionRow>}
      {enabled && game && perGame && acSeparate && <PanelSectionRow>
        <DropdownItem
          label="Edit settings for"
          description={sourceDescription}
          selectedOption={editingAc ? "charger" : "battery"}
          rgOptions={[{ data: "battery", label: "Battery" }, { data: "charger", label: "Charger" }]}
          disabled={cpuBusy || loading}
          onChange={option => {
            if ((option.data === "charger") === editingAc || cpuBusyOwnersRef.current.size) return;
            askDiscard(() => { if (!cpuBusyOwnersRef.current.size) setEditingAc(option.data === "charger"); });
          }}
        />
      </PanelSectionRow>}
      {enabled && PRESET_ORDER.map(key => (
        <PanelSectionRow key={key}>
          <ButtonItem
            layout="below"
            description={preset === key ? presetDescription : undefined}
            disabled={preset === key || loading || !tdpAvailable}
            onClick={() => void handlePresetChange(key)}
          >
            {preset === key ? `> ${PRESET_LABELS[key]}` : PRESET_LABELS[key]}
          </ButtonItem>
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
};

const customLimitsSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, dirty,
    setDirty, dirtyRef, draftBaseRef, active, setActive, caps, customCaps, minW, advancedTdpControl, loading,
    tdpAvailable, operationLoading, editingAc, perGame, game, acSeparate, setTuning, setAcTuning, setSavedPreset,
    setSavedAcPreset, setGlobalProfile } = s;
  const om      = offsetMax(active.spl, caps);
  const editActive = (next: Tuning) => {
    if (!dirtyRef.current) draftBaseRef.current = { ac: editingAc, tuning: active };
    dirtyRef.current = true;
    setDirty(true);
    setActive(next);
  };
  const handlers = {
    onSpl: (v: number) => editActive(withSpl(active, v, customCaps, minW)),
    onSppt: (v: number) => editActive(withSppt(active, v, customCaps)),
    onFppt: (v: number) => editActive(withFppt(active, v, customCaps)),
  };

  // ── Apply (Custom mode only) ──────────────────────────────────────────────────
  const apply = async () => {
    const op = beginOperation();
    if (!op) return;
    const appId = (perGame && game) ? game.appId : "";
    const target = advancedTdpControl ? active : simpleTuning(active.spl, customCaps, minW);
    const a = absolute(target);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, "custom");
        if (!operationCurrent(op)) return;
        if (!r.success) throw new Error(r.stderr || "The limits were not saved.");
        setAcTuning(target);
        setSavedAcPreset("custom");
        clearDraft();
        done(`Saved for ${game!.name} on charger.`);
      } else {
        const r = await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, "custom",
          op.appId);
        if (!operationCurrent(op)) return;
        if (!r.success) throw new Error(r.stderr || "The limits were not applied.");
        setTuning(target);
        if (!appId) setGlobalProfile({ ...a, preset: "custom" });
        else setSavedPreset("custom");
        clearDraft();
        done(appId ? `Saved for ${game!.name}.` : "Custom limits applied.");
      }
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("apply", e);
    } finally { finishOperation(op); }
  };

  return (
    <PanelSection title={`Custom limits${dirty ? " · unsaved" : ""}`}>
      <PanelSectionRow>
        <SliderField
          label={`Sustained power · ${active.spl} W`}
          value={active.spl} min={minW} max={customCaps.spl} step={1}
          disabled={loading || !tdpAvailable}
          onChange={advancedTdpControl ? handlers.onSpl
            : (value) => editActive(simpleTuning(value, customCaps, minW))}
          description={advancedTdpControl ? undefined : "Boost follows automatically."}
        />
      </PanelSectionRow>
      {advancedTdpControl && <>
      <PanelSectionRow>
        <SliderField
          label={`Slow boost · +${active.spptOff} W`}
          value={active.spptOff} min={0} max={om.sppt || 1} step={1}
          disabled={om.sppt === 0 || loading || !tdpAvailable}
          onChange={handlers.onSppt}
          description={boostRoomNote(active.spl, active.spptOff, om.sppt)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label={`Fast boost · +${active.fpptOff} W`}
          value={active.fpptOff} min={0} max={om.fppt || 1} step={1}
          disabled={om.fppt === 0 || loading || !tdpAvailable}
          onChange={handlers.onFppt}
          description={boostRoomNote(active.spl, active.fpptOff, om.fppt)}
        />
      </PanelSectionRow>
      </>}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={apply} disabled={loading || !tdpAvailable}
          description={failureFor("apply") ?? (dirty ? "Unsaved changes" : undefined)}>
          {operationLoading ? "Applying…"
            : editingAc && game ? `Apply for ${game.name} on charger`
            : perGame && game ? `Apply for ${game.name}`
            : "Apply"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

const moreSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, setFailure,
    readCaps, adoptCaps, capsNow, advancedTdpControl, setAdvancedTdpControl, extrasUnlocked, setExtrasUnlocked,
    extrasAvailable, caps, stdCaps, minW, presets, tuning, setTuning, acTuning, setAcTuning, setPreset, perGame,
    setPerGame, setProfileExists, profileExists, setAcSeparate, acSeparate, setEditingAc, game, enabled,
    savedPreset, setSavedPreset, savedAcPreset, setSavedAcPreset, globalProfile, setGlobalProfile,
    adoptGlobalSettings, loading, tdpAvailable, operationLoading, details, setDetails, info, cpuNote,
    separateBattery, setSeparateBattery } = s;
  const deleteProfile = async () => {
    if (!game) return;
    const op = beginOperation();
    if (!op) return;
    const app = game;
    try {
      const deleted = await deleteGameProfile(app.appId);
      if (!operationCurrent(op)) return;
      if (!deleted.success) throw new Error(deleted.stderr || "Could not delete the profile.");
      setPerGame(false); setProfileExists(false); setAcSeparate(false); setEditingAc(false);
      setSavedPreset(undefined); setSavedAcPreset(undefined);
      const s = await getSettings();
      if (!operationCurrent(op)) return;
      adoptGlobalSettings(s);
      done(`Profile for ${app.name} deleted.`);
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("delete", e);
    } finally { finishOperation(op); }
  };

  // ── Extras: unlock extended TDP range ────────────────────────────────────────
  const handleExtrasUnlockedToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
    try {
      const result = await setExtrasUnlockedCall(checked);
      if (!operationCurrent(op)) return;
      if (!result.success) throw new Error(result.stderr || "Could not change the 50 W setting.");
      setExtrasUnlocked(checked);
      setFailure(null);
      // Enabling may have just downloaded the helper, which widens the caps.
      if (checked) { const c = await readCaps(); if (operationCurrent(op)) adoptCaps(c); }
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("extras", e);
      return;
    }
    if (checked) return;

    // The backend clamps every persisted target and the active hardware change
    // in one transaction. Mirror that result locally without issuing a second
    // apply that could race a game or charger transition.
    const battery = absolute(tuning), charging = absolute(acTuning);
    const batteryCustom = resolvedPreset(battery.spl, battery.sppt, battery.fppt,
      perGame ? savedPreset : globalProfile.preset, presets) === "custom";
    const acCustom = resolvedPreset(charging.spl, charging.sppt, charging.fppt,
      savedAcPreset, presets) === "custom";
    const t = !advancedTdpControl && batteryCustom
      ? simpleTuning(tuning.spl, simpleCaps(stdCaps, false), minW)
      : normalise(tuning, stdCaps, minW);
    const at = !advancedTdpControl && acCustom
      ? simpleTuning(acTuning.spl, simpleCaps(stdCaps, false), minW)
      : normalise(acTuning, stdCaps, minW);
    const tChanged = t.spl !== tuning.spl || t.spptOff !== tuning.spptOff ||
      t.fpptOff !== tuning.fpptOff;
    const atChanged = at.spl !== acTuning.spl || at.spptOff !== acTuning.spptOff ||
      at.fpptOff !== acTuning.fpptOff;
    setTuning(t);
    setAcTuning(at);
    setGlobalProfile((current) => {
      const clamped = normalise(
        fromAbsolute(current.spl, current.sppt, current.fppt), stdCaps, minW);
      const values = absolute(clamped);
      return values.spl !== current.spl || values.sppt !== current.sppt || values.fppt !== current.fppt
        ? { ...values, preset: "custom" }
        : current;
    });
    if (tChanged) {
      setPreset("custom");
      if (perGame) setSavedPreset("custom");
    }
    if (acSeparate && atChanged) setSavedAcPreset("custom");
    } finally { finishOperation(op); }
  };

  const handleAdvancedTdpControlToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
    try {
      const result = await setAdvancedTdpControlCall(checked);
      if (!operationCurrent(op)) return;
      if (!result.success) throw new Error(result.stderr || "Could not change Separate boost limits.");
      setAdvancedTdpControl(checked);
      setFailure(null);
      clearDraft();
    } catch (error) {
      if (operationCurrent(op)) showError("advanced", error);
      return;
    }
    if (!checked) {
      const limits = simpleCaps(caps, extrasUnlocked && extrasAvailable);
      const battery = absolute(tuning), charging = absolute(acTuning);
      if (resolvedPreset(battery.spl, battery.sppt, battery.fppt,
          perGame ? savedPreset : globalProfile.preset, presets) === "custom")
        setTuning(simpleTuning(tuning.spl, limits, minW));
      if (resolvedPreset(charging.spl, charging.sppt, charging.fppt,
          savedAcPreset, presets) === "custom")
        setAcTuning(simpleTuning(acTuning.spl, limits, minW));
      setGlobalProfile((current) => {
        if (resolvedPreset(current.spl, current.sppt, current.fppt, current.preset, presets) !== "custom")
          return current;
        return { ...absolute(simpleTuning(current.spl,
          simpleCaps(caps, extrasUnlocked && extrasAvailable), minW)), preset: current.preset };
      });
    } else {
      try {
        const settings = await getSettings();
        if (!operationCurrent(op)) return;
        const global = fromAbsolute(toW(settings.spl), toW(settings.sppt), toW(settings.fppt));
        setGlobalProfile({ ...absolute(global), preset: settings.active_preset });
        if (perGame && game) {
          const appId = game.appId;
          const profile = await getGameProfile(appId);
          if (!operationCurrent(op)) return;
          if (profile.exists) {
            const p = profile.profile;
            const ac = profile.ac_profile;
            setTuning(fromAbsolute(toW(p.spl), toW(p.sppt), toW(p.fppt)));
            setAcTuning(fromAbsolute(toW(ac.spl), toW(ac.sppt), toW(ac.fppt)));
          }
        } else {
          setTuning(global);
        }
      } catch (error) {
        if (operationCurrent(op)) showError("advanced", error);
      }
    }
    } finally { finishOperation(op); }
  };

  const handleSeparateBatteryToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
      const result = await setSeparateBatteryPresetsCall(checked);
      if (!operationCurrent(op)) return;
      if (!result.success) throw new Error(result.stderr || `Could not change ${BATTERY_PRESETS_LABEL}.`);
      setSeparateBattery(checked);
      setFailure(null);
    } catch (error) {
      if (operationCurrent(op)) showError("battery", error);
    } finally { finishOperation(op); }
  };

  const handleHelperRetry = async () => {
    const op = beginOperation(false, false);
    if (!op) return;
    try {
      const result = await retryExtras();
      if (!operationCurrent(op)) return;
      if (!result.success) throw new Error(result.error || "Retry failed");
      const c = await readCaps();
      if (operationCurrent(op)) { adoptCaps(c); setFailure(null); }
    } catch (error) {
      if (operationCurrent(op)) showError("helper", error);
    } finally { finishOperation(op); }
  };

  return (
    <PanelSection title="More">
      <PanelSectionRow>
        <ToggleField
          label="Separate boost limits"
          description={failureFor("advanced") ?? "Set slow and fast boost yourself in Custom."}
          checked={advancedTdpControl}
          disabled={loading || !tdpAvailable}
          onChange={handleAdvancedTdpControlToggle}
        />
      </PanelSectionRow>
      {extrasShown(capsNow) && <PanelSectionRow>
        <ToggleField
          label={`Allow up to ${FALLBACK_MAX.fppt} W`}
          description={failureFor("extras") ?? extrasNote(extrasUnlocked, capsNow, stdCaps)}
          checked={extrasUnlocked}
          disabled={loading || !tdpAvailable}
          onChange={checked => checked ? confirmAction(extrasConfirm(capsNow).title, extrasConfirm(capsNow).text,
            extrasConfirm(capsNow).ok, () => void handleExtrasUnlockedToggle(true))
            : void handleExtrasUnlockedToggle(false)}
        />
      </PanelSectionRow>}
      {tdpAvailable && <PanelSectionRow>
        <ToggleField
          label={BATTERY_PRESETS_LABEL}
          description={failureFor("battery") ?? BATTERY_PRESETS_NOTE}
          checked={separateBattery}
          disabled={loading}
          onChange={checked => void handleSeparateBatteryToggle(checked)}
        />
      </PanelSectionRow>}
      {enabled && game && profileExists && <PanelSectionRow>
        <ButtonItem layout="below" disabled={loading} description={failureFor("delete")}
          onClick={() => confirmAction(`Delete profile for ${game.name}?`,
            `Its power, CPU Boost and energy settings are removed. ${game.name} will use Global settings.`,
            "Delete", () => void deleteProfile())}>
          {`Delete profile for ${game.name}`}
        </ButtonItem>
      </PanelSectionRow>}
      {helperRetryShown(capsNow, extrasUnlocked) && <PanelSectionRow>
        <ButtonItem layout="below" disabled={operationLoading} description={failureFor("helper")}
          onClick={handleHelperRetry}>Retry helper download</ButtonItem>
      </PanelSectionRow>}
      {detailsRows(details, () => setDetails(value => !value), [
        ["Limits now", limitsText(info)],
        ["Set by", sourceText(info)],
        ...(cpuNote ? [["CPU energy", cpuNote] as [string, string]] : []),
      ])}
    </PanelSection>
  );
};

export const TdpPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const s = useTdpState(tabShown);
  const { ready, setupErr, initializing, initialize, enabled, preset, profilePending, profileError, game, perGame,
    acSeparate, editingAc, acOnline, onCpuBusyChange, setCpuNote } = s;

  if (!ready && setupErr) return (
    <PanelSection title="TDP status unavailable">
      {infoRow("Error", setupErr)}
      <PanelSectionRow><ButtonItem disabled={initializing} onClick={() => void initialize()}>
        Retry status
      </ButtonItem></PanelSectionRow>
    </PanelSection>
  );

  if (!ready) return (
    <PanelSection title="Initializing...">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  return (
    <>
      {noticeSection(s)}

      {powerSection(s)}

      {enabled && preset === "custom" && customLimitsSection(s)}

      {!profilePending && !profileError && <CpuPowerControlsSection
        key={`${enabled}:${game?.appId ?? ""}:${perGame}:${acSeparate && editingAc}`}
        appId={enabled && perGame && game ? game.appId : ""}
        acProfile={!!(enabled && perGame && game && acSeparate && editingAc)}
        expectedAppId={game?.appId ?? ""}
        powerSource={acOnline}
        onBusyChange={onCpuBusyChange}
        onCompatibilityNote={setCpuNote}
      />}

      {moreSection(s)}
    </>
  );
};

// ── Plugin entry point ─────────────────────────────────────────────────────────

export const startTdpWatcher = () => AppWatcher.start();
export const stopTdpWatcher = () => AppWatcher.stop();
