// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP
// TDP page: live readings and the page state.

import { addEventListener, removeEventListener, useQuickAccessVisible } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "./shared";
import {
  absolute, capsState, FALLBACK_MAX, FALLBACK_MIN, FALLBACK_STD, fromAbsolute, PRESETS, resolvedPreset, runningNow,
  simpleTuning, type Caps, type CapsInfo, type PresetKey, type PresetTable, type RunningLimits, type TdpInfo,
  type Tuning,
} from "../desktop/native/tdp.mjs";
import {
  type GameProfile, type PowerSource, type RunningGame, type Settings, getCaps, getExtrasUnlocked, getGameProfile,
  getPowerSource, getSettings, getTdpInfo, isReady, notify, notifyFailure, savedTuning, setPanelActive, toW
} from "./tdp_api";
import { AppWatcher } from "./tdp_watcher";

// ── Live TDP readings ──────────────────────────────────────────────────────────

// Must stay comfortably under _PANEL_ACTIVE_TTL_S in main.py (90 s).
export const PANEL_LEASE_MS = 30000;

/** Live limits and package draw while the page is shown; null until the first reading. */
export const useTdpInfo = ({ visible: tabShown = true }: { visible?: boolean } = {}) => {
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

// ── Main content ───────────────────────────────────────────────────────────────
export type Scope = "global" | "game" | "game_ac";

/** State, effects and shared operations of the TDP page; the sections below render from them. */
export const useTdpState = (tabShown: boolean) => {
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
  // What runs now, as the backend names it (get_settings.running); undefined until read or from an older backend.
  const [running, setRunning] = useState<RunningLimits | undefined>(undefined);
  const [runningRevision, setRunningRevision] = useState(0);

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
    if (pageMountedRef.current) { setLoading(false); setRunningRevision((n) => n + 1); }
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
    const battery = savedTuning(p, storedPreset, presets, advancedTdpControl, caps, minW);
    clearDraft();
    setPerGame(true);
    setProfileExists(true);
    setTuning(battery.tuning);
    setAcTuning(savedTuning(ac, ac.ac_preset, presets, advancedTdpControl, caps, minW).tuning);
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
            ? simpleTuning(w, unlocked ? c.max : c.std, c.min)
            : fromAbsolute(w, sw, fw);
          setTuning(view);
          setGlobalProfile({ ...absolute(view), preset: s.active_preset || undefined });
          setPreset(selected);
          setEnabled(s.enabled !== false);
          setAdvancedTdpControl(s.advanced_tdp_control !== false);
          setSeparateBattery(s.separate_battery_presets === true);
          setRunning(runningNow(s) ?? undefined);
          if (powerRevision === powerSourceRevisionRef.current) setAcOnline(ps.ac);
          initialPowerReadRef.current = true;
          noGameSyncedRef.current = AppWatcher.currentGame() === null;
          setExtrasUnlocked(eu);
          setSetupErr(null);
          readyRef.current = true;
          setReady(true);
        } else {
          setSetupErr("TDP and CPU controls are still starting. Try again if this continues.");
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
  // while the page is open. The same read keeps what runs now: it is re-read after every
  // action and power-source change and when a game's profile is shown too.
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
        if (active && !operationBusyRef.current && operation === operationRef.current) {
          setSeparateBattery(s.separate_battery_presets === true);
          setRunning(runningNow(s) ?? undefined);
        }
      }).catch(() => undefined).finally(() => { reading = false; });
    };
    if (batteryReadRef.current) read(); else batteryReadRef.current = true;
    const timer = setInterval(read, 10000);
    return () => { active = false; clearInterval(timer); };
  }, [ready, visible, runningRevision, acOnline]);

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
      const { preset: selected, tuning: view } = savedTuning(s, s.active_preset, presets, advancedTdpControl, caps, minW);
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
      setRunning(runningNow(s) ?? undefined);
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
        if (gp.exists && !gp.profile) throw new Error("Game profile data is missing or corrupt.");
        // An unused profile is kept for later; the game runs on Global settings.
        if (gp.exists && gp.active !== false) { showGameProfile(gp); setRunningRevision((n) => n + 1); }
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
    separateBattery, setSeparateBattery, batteryPresets, running, setSavedPreset, savedAcPreset, setSavedAcPreset, operationLoading, profilePending, setProfilePending,
    profileError, setProfileError, setProfileAttempt, loading, cpuBusy, setDirty, dirty, dirtyRef, draftBaseRef,
    setFailure, details, setDetails, cpuNote, setCpuNote, info, cpuBusyOwnersRef, onCpuBusyChange, beginOperation,
    operationCurrent, finishOperation, readCaps, adoptCaps, caps, active, setActive, clearDraft, done,
    showError, failureFor, showGameProfile, adoptGlobalSettings, capsNow };
};
export type TdpState = ReturnType<typeof useTdpState>;

// Sections are plain render functions without hooks, called inline by TdpPage:
// the page keeps one element tree and one hook order.
