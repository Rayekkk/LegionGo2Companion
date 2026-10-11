// SPDX-License-Identifier: BSD-3-Clause AND MIT
// Copyright (c) 2026 Rayekkk
// Portions copyright (c) 2026 piyush-tyagi-13 and M4ttiA, MIT - see LICENSE.MIT
// https://github.com/Rayekkk/LeGo-Vibe-Control

import {
  ButtonItem,
  Field,
  findModuleExport,
  Focusable,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  Spinner,
  ToggleField,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  removeEventListener,
  toaster,
  useQuickAccessVisible,
} from "@decky/api";
import { useState, useEffect, useCallback, useRef } from "react";

// ── Types ─────────────────────────────────────────────────────────────────────

interface VibeSettings {
  level: number;
  mode: number;
  touchpadIntensity: number;
  touchpadEnabled: boolean;
}

interface SettingsResponse {
  settings: VibeSettings;
  app_id: string;
  profile_id: string;
  overwrite: boolean;
}

interface ApplyResponse {
  success: boolean;
  settings: VibeSettings;
  error?: string;
  app_id?: string;
  profile_id?: string;
  overwrite?: boolean;
}

interface DriverStatus {
  found: boolean;
  paths: string[];
  method: string;
  ids: string;
}

interface ReadyState {
  ready: boolean;
  error: string;
}

// ── Constants ─────────────────────────────────────────────────────────────────

const DEFAULT_APP = "0";
const APP_HEARTBEAT_MS = 6000;

const LEVEL_LABELS = ["Off", "Low", "Medium", "High"];
const LEVEL_NOTCHES = ["Off", "Low", "Med", "High"];

// Display names for the driver's raw mode values. Anything the driver
// reports that is not listed here is title-cased at render time, so a new
// kernel mode shows up without needing a plugin release.
const MODE_LABELS: Record<string, string> = {
  fps: "FPS",
  racing: "Racing",
  standard: "Standard",
  spg: "SPG",
  rpg: "RPG",
};
const MODE_NOTCHES: Record<string, string> = {
  fps: "FPS",
  racing: "Race",
  standard: "Std",
  spg: "SPG",
  rpg: "RPG",
};

const FALLBACK_MODES = ["fps", "racing", "standard", "spg", "rpg"];

const titleCase = (raw: string) => raw.charAt(0).toUpperCase() + raw.slice(1);
const modeLabel = (raw: string) => MODE_LABELS[raw] ?? titleCase(raw);
const modeNotch = (raw: string) => MODE_NOTCHES[raw] ?? titleCase(raw).slice(0, 5);

// ── Backend callables ─────────────────────────────────────────────────────────

const isReady = callable<[], ReadyState>("vibe_is_ready");
const getSettings = callable<[], SettingsResponse>("vibe_get_settings");
const setActiveApp = callable<[string], SettingsResponse & { success: boolean; changed: boolean }>("vibe_set_active_app");

const setIntensity = callable<[number, string, string], ApplyResponse>("vibe_set_intensity");
const setRumbleMode = callable<[number, string, string], ApplyResponse>("vibe_set_rumble_mode");
const setTouchpadIntensity = callable<[number, string, string], ApplyResponse>("vibe_set_touchpad_intensity");
const setTouchpadEnabled = callable<[boolean, string, string], ApplyResponse>("vibe_set_touchpad_enabled");
const resetToDefault = callable<[string, string], ApplyResponse>("vibe_reset_to_default");
const reapply = callable<[], ApplyResponse>("vibe_reapply");

const setProfileOverwrite = callable<[string, boolean, string], ApplyResponse & { overwrite: boolean }>("vibe_set_profile_overwrite");

const getDriverStatus = callable<[], DriverStatus>("vibe_get_driver_status");
const getCapabilities = callable<[], { intensity: string[]; mode: string[]; tp_intensity: string[] }>("vibe_get_capabilities");
const testVibration = callable<[number], { success: boolean; error?: string }>("vibe_test_vibration");

// ── Toasts ────────────────────────────────────────────────────────────────────

const notify = (title: string, body: string) => {
  try {
    toaster.toast({ title, body, duration: 4000 });
  } catch {
    console.error(`[lego-vibe] ${title}: ${body}`);
  }
};

const notifyFailure = (title: string, err: unknown) => {
  const body = err instanceof Error ? err.message : String(err ?? "Unknown error");
  console.error(`[lego-vibe] ${title}`, err);
  notify(title, body);
};

/** Report a backend result that carries its own error string. */
const checkResult = (title: string, res: { success: boolean; error?: string }) => {
  if (!res.success) notify(title, res.error ?? "The driver rejected the change");
  return res.success;
};

// ── Resume from suspend ───────────────────────────────────────────────────────

/**
 * Subscribe to resume-from-suspend. Returns an unsubscribe function, or null
 * when the client offers no way to hear about it.
 *
 * `SteamClient.System.RegisterForOnResumeFromSuspend` was removed from the
 * Steam client in the September 2025 beta. Optional chaining meant calling it
 * silently did nothing - confirmed on the device, where two suspend cycles
 * produced no reapply at all. Nothing looked broken only because the
 * controller happened to re-enumerate and the hotplug monitor caught it; on a
 * resume where it does not, the write cache still believes our values are in
 * place and the controller quietly keeps the firmware defaults.
 *
 * The replacement lives on a SleepManager module, reachable either as a global
 * or through the webpack exports; the legacy call stays for older clients.
 */
function onResumeFromSuspend(handler: () => void): (() => void) | null {
  const asUnsub = (reg: any): (() => void) | null => {
    if (typeof reg === "function") return reg;
    if (typeof reg?.unregister === "function") return () => reg.unregister();
    return null;
  };
  const isSleepManager = (e: any) =>
    !!e && typeof e === "object" &&
    (typeof e.RegisterForNotifyResumeFromSuspend === "function" ||
      typeof e.NotifyResumeFromSuspend === "function");

  try {
    const mgr = (window as any).SleepManager ?? findModuleExport(isSleepManager);
    const unsub = asUnsub(mgr?.RegisterForNotifyResumeFromSuspend?.(handler));
    if (unsub) return unsub;
  } catch (e) {
    console.warn("[lego-vibe] SleepManager lookup failed", e);
  }

  try {
    const unsub = asUnsub(
      (window as any).SteamClient?.System?.RegisterForOnResumeFromSuspend?.(handler));
    if (unsub) return unsub;
  } catch (e) {
    console.warn("[lego-vibe] legacy resume registration failed", e);
  }

  // Said out loud rather than swallowed: this going quiet again is exactly how
  // the previous registration rotted unnoticed.
  console.warn("[lego-vibe] no resume-from-suspend notification available; "
    + "settings will be restored by the backend resume and drift checks");
  return null;
}

// ── Running app watcher ───────────────────────────────────────────────────────

type SettingsListener = (res: SettingsResponse, readEpoch?: number) => void;
let vibeReadEpoch = 0;

/**
 * Tracks the foreground game and tells the backend about it, so the backend
 * can resolve per-game profiles on its own - including for hotplug and
 * resume, which never go through the UI.
 *
 * This used to poll every 100 ms. It now reacts to Steam's app lifetime
 * notifications, with a slow interval purely as a safety net because
 * Router.MainRunningApp can change without a lifetime event firing.
 */
class AppWatcher {
  private static listeners: SettingsListener[] = [];
  private static currentId = DEFAULT_APP;
  private static timer: ReturnType<typeof setInterval> | undefined;
  private static unsubs: Array<() => void> = [];
  private static started = false;
  private static busy = false;
  private static lastPush = 0;
  private static generation = 0;
  private static delayedChecks = new Set<ReturnType<typeof setTimeout>>();

  static activeId(): string {
    try {
      return String((Router as any)?.MainRunningApp?.appid || DEFAULT_APP);
    } catch {
      return DEFAULT_APP;
    }
  }

  static displayName(): string {
    try {
      const app = (Router as any)?.MainRunningApp;
      return app?.appid ? (app.display_name || `App ${app.appid}`) : "";
    } catch {
      return "";
    }
  }

  static listen(fn: SettingsListener): () => void {
    this.listeners.push(fn);
    return () => {
      this.listeners = this.listeners.filter((f) => f !== fn);
    };
  }

  static start() {
    if (this.started) return;
    this.started = true;
    const generation = ++this.generation;
    this.currentId = this.activeId();

    const steam = (window as any).SteamClient;

    try {
      const reg = steam?.GameSessions?.RegisterForAppLifetimeNotifications?.(() => {
        // Router.MainRunningApp lags the notification slightly.
        if (!this.started || generation !== this.generation) return;
        const timer = setTimeout(() => {
          this.delayedChecks.delete(timer);
          if (generation === this.generation) void this.check();
        }, 300);
        this.delayedChecks.add(timer);
      });
      if (reg?.unregister) this.unsubs.push(() => reg.unregister());
    } catch (e) {
      console.warn("[lego-vibe] app lifetime notifications unavailable", e);
    }

    // The controller comes back at its firmware defaults, and the backend's
    // write cache would otherwise skip the rewrite.
    const offResume = onResumeFromSuspend(() => {
      if (!this.started || generation !== this.generation) return;
      void reapply()
        .then((res) => {
          if (!res.success) console.warn("[lego-vibe] reapply after resume failed");
        })
        .catch((e) => console.error("[lego-vibe] reapply after resume threw", e));
    });
    if (offResume) this.unsubs.push(offResume);

    this.timer = setInterval(() => void this.check(), 2000);
    void this.check();
  }

  static stop() {
    this.started = false;
    this.generation += 1;
    this.busy = false;
    for (const timer of this.delayedChecks) clearTimeout(timer);
    this.delayedChecks.clear();
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
    for (const off of this.unsubs) {
      try {
        off();
      } catch {
        /* the subscription may already be gone */
      }
    }
    this.unsubs = [];
    this.listeners = [];
    this.currentId = DEFAULT_APP;
    this.lastPush = 0;
    this.started = false;
  }

  private static async check() {
    if (!this.started || this.busy) return;
    const generation = this.generation;
    const id = this.activeId();
    const changed = id !== this.currentId;
    const now = Date.now();
    if (!changed && now - this.lastPush < APP_HEARTBEAT_MS) return;
    this.busy = true;
    const readEpoch = vibeReadEpoch;
    try {
      const res = await setActiveApp(id);
      if (!this.started || generation !== this.generation) return;
      if (this.activeId() !== id) return;
      // Committed only once the backend has it. Recording the id before the
      // call meant a single failed RPC - the loader restarting, say - left
      // every later tick thinking there was nothing to send, so the hardware
      // kept the previous game's profile for the rest of the session.
      this.currentId = id;
      this.lastPush = now;
      this.listeners.forEach((fn) => fn(res, readEpoch));
    } catch (e) {
      console.error("[lego-vibe] setActiveApp failed, will retry", e);
    } finally {
      if (generation === this.generation) {
        this.busy = false;
        // A game changed while the previous RPC was busy. Deliver its profile
        // immediately instead of waiting for the next two-second timer tick.
        if (this.started && this.activeId() !== id) void this.check();
      }
    }
  }
}

// ── Styles - Steam theme variables with hardcoded fallbacks ───────────────────

const OK_COLOR = "var(--gpColor-Green, #4ade80)";
const BAD_COLOR = "var(--gpColor-Red, #f87171)";
const WARN_COLOR = "var(--gpColor-Yellow, #fbbf24)";
const DIM_COLOR = "var(--gpColor-TextMuted, rgba(255,255,255,0.5))";

const styles = {
  container: { display: "flex", flexDirection: "column" as const, gap: "4px" },
  statusRow: { display: "flex", alignItems: "center", gap: "8px", padding: "4px 0" },
  dot: (ok: boolean) => ({
    width: "8px",
    height: "8px",
    borderRadius: "50%",
    backgroundColor: ok ? OK_COLOR : BAD_COLOR,
    flexShrink: 0,
  }),
  statusText: (ok: boolean) => ({
    fontSize: "11px",
    color: ok ? OK_COLOR : BAD_COLOR,
    wordBreak: "break-all" as const,
  }),
  valueTag: {
    fontSize: "13px",
    fontWeight: "bold",
    color: "var(--gpColor-White, #fff)",
    background: "rgba(255,255,255,0.1)",
    borderRadius: "4px",
    padding: "1px 6px",
  },
  infoBox: {
    background: "rgba(251,191,36,0.15)",
    border: "1px solid rgba(251,191,36,0.4)",
    borderRadius: "6px",
    padding: "8px 10px",
    fontSize: "11px",
    color: WARN_COLOR,
    lineHeight: "1.5",
    marginTop: "4px",
  },
  errorBox: {
    background: "rgba(248,113,113,0.1)",
    border: "1px solid rgba(248,113,113,0.4)",
    borderRadius: "6px",
    padding: "8px 10px",
    fontSize: "11px",
    color: BAD_COLOR,
    lineHeight: "1.5",
    marginTop: "4px",
  },
  methodText: {
    fontSize: "10px",
    color: DIM_COLOR,
    marginTop: "2px",
  },
  profileTag: {
    fontSize: "11px",
    fontWeight: "bold",
    color: "var(--gpColor-White, #fff)",
    background: "rgba(74,222,128,0.25)",
    border: "1px solid rgba(74,222,128,0.5)",
    borderRadius: "3px",
    padding: "0px 5px",
  },
};

// ── Main component ────────────────────────────────────────────────────────────

const DEFAULT_SETTINGS: VibeSettings = {
  level: 2,
  mode: 0,
  touchpadIntensity: 2,
  touchpadEnabled: true,
};

let vibeMutationChain: Promise<unknown> = Promise.resolve();
let vibeMutationsPending = 0;

const LGoVibeControl = () => {
  const [settings, setSettings] = useState<VibeSettings>(DEFAULT_SETTINGS);
  const [modes, setModes] = useState<string[]>(FALLBACK_MODES);
  const [driver, setDriver] = useState<DriverStatus | null>(null);
  const [appId, setAppId] = useState(DEFAULT_APP);
  const [gameName, setGameName] = useState("");
  const [perGameOn, setPerGameOn] = useState(false);

  const [loading, setLoading] = useState(true);
  const [setupErr, setSetupErr] = useState<string | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [applying, setApplying] = useState(false);
  const [profileChanging, setProfileChanging] = useState(false);
  const [testing, setTesting] = useState(false);
  const [showDriverDetails, setShowDriverDetails] = useState(false);

  const visible = useQuickAccessVisible();
  const initialDriverReadRef = useRef(false);
  const initialLoadRef = useRef<Promise<void> | null>(null);
  const driverRevisionRef = useRef(0);
  const mounted = useRef(true);
  const currentApp = useRef(DEFAULT_APP);
  const gameRevision = useRef(0);
  const mutationsPending = useRef(0);
  const controlActionPending = useRef(false);
  const testPending = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  // Coalesces a slider drag into a single backend call. The UI still moves
  // immediately; only the RPC and its disk commit are deferred.
  const timers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});
  const pendingWrites = useRef<Record<string, () => void>>({});
  const writeVersions = useRef<Record<string, number>>({});
  const flushPendingWrites = useCallback(() => {
    for (const timer of Object.values(timers.current)) clearTimeout(timer);
    timers.current = {};
    const writes = Object.values(pendingWrites.current);
    pendingWrites.current = {};
    for (const flush of writes) flush();
  }, []);
  const debounce = useCallback((key: string, fn: () => void, delay = 150) => {
    const pending = timers.current[key];
    if (pending) clearTimeout(pending);
    const version = (writeVersions.current[key] ?? 0) + 1;
    writeVersions.current[key] = version;
    let flushed = false;
    const flush = () => {
      // A cleared browser callback may already be queued. It must neither
      // replay a flushed gesture after Reset nor save a superseded slider value.
      if (flushed || writeVersions.current[key] !== version) return;
      flushed = true;
      delete timers.current[key];
      delete pendingWrites.current[key];
      fn();
    };
    pendingWrites.current[key] = flush;
    timers.current[key] = setTimeout(flush, delay);
  }, []);

  useEffect(() => flushPendingWrites, [flushPendingWrites]);

  // Bumped by every optimistic edit. A reply may only overwrite the UI while it
  // is still the newest thing that happened, otherwise a slow sysfs write snaps
  // the slider back to a value the user has already moved off.
  const editSeq = useRef(0);
  const latestFieldEdit = useRef<Record<string, number>>({});

  const adoptResponse = useCallback((res: SettingsResponse, readEpoch?: number) => {
    if (!mounted.current) return;
    const changedGame = currentApp.current !== res.app_id;
    if (!changedGame && readEpoch !== undefined && readEpoch !== vibeReadEpoch) return;
    // A heartbeat for the same game can carry a snapshot from before a queued
    // edit. Keep the draft until that edit answers; a new game always wins.
    if (!changedGame && (mutationsPending.current || Object.keys(pendingWrites.current).length)) return;
    if (changedGame) gameRevision.current += 1;
    currentApp.current = res.app_id;
    // Counts as an edit: switching game replaces the whole profile, and a field
    // reply still in flight from the previous one must not undo that.
    editSeq.current += 1;
    setSettings(res.settings);
    setAppId(res.app_id);
    setPerGameOn(res.overwrite);
    setGameName(AppWatcher.displayName());
  }, []);

  const enqueueMutation = useCallback(<T,>(operation: () => Promise<T>) => {
    vibeReadEpoch += 1;
    mutationsPending.current += 1;
    vibeMutationsPending += 1;
    if (mounted.current) setApplying(true);
    const request = vibeMutationChain.then(operation, operation);
    vibeMutationChain = request.catch(() => undefined);
    return request.finally(() => {
      vibeReadEpoch += 1;
      mutationsPending.current -= 1;
      vibeMutationsPending -= 1;
      if (mounted.current) setApplying(mutationsPending.current > 0);
    });
  }, []);

  const refreshDriver = useCallback(async () => {
    const revision = driverRevisionRef.current;
    try {
      const status = await getDriverStatus();
      if (mounted.current && revision === driverRevisionRef.current) setDriver(status);
    } catch (e) {
      console.error("[lego-vibe] getDriverStatus failed", e);
    }
  }, []);

  // Initial load. Gated on is_ready so a backend that failed to start shows the
  // reason instead of sliders that silently do nothing.
  useEffect(() => {
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      if (initialLoadRef.current) await initialLoadRef.current;
      if (!active) return;
      const request = (async () => {
        try {
          const state = await isReady();
          if (!active) return;
          if (state.error) {
            setSetupErr(state.error);
            setLoading(false);
            return;
          }
          if (!state.ready) {
            if (active) retryTimer = setTimeout(check, 1000);
            return;
          }
          if (vibeMutationsPending) await vibeMutationChain;
          if (!active) return;
          const driverRevision = driverRevisionRef.current;
          const settingsRevision = editSeq.current;
          const reads = [
            getSettings(),
            getDriverStatus(),
            getCapabilities().catch(() => ({ mode: FALLBACK_MODES })),
          ] as const;
          let snapshot: [SettingsResponse, DriverStatus, { mode: string[] }];
          try { snapshot = await Promise.all(reads); }
          catch (error) {
            if (active) {
              setLoadError(error instanceof Error ? error.message : "Vibration settings could not be loaded. Check the Decky connection and retry.");
              setLoading(false);
            }
            // A rejection does not finish the other RPCs. Keep a retry behind
            // this attempt instead of filling the backend queue with duplicates.
            await Promise.allSettled(reads);
            throw error;
          }
          const [current, status, caps] = snapshot;
          if (!active) return;
          if (settingsRevision === editSeq.current) adoptResponse(current);
          if (driverRevision === driverRevisionRef.current) setDriver(status);
          initialDriverReadRef.current = true;
          setModes(caps?.mode?.length ? caps.mode : FALLBACK_MODES);
          setLoadError("");
          setLoading(false);
        } catch (e) {
          if (!active) return;
          setLoadError(e instanceof Error ? e.message : "Vibration settings could not be loaded. Check the Decky connection and retry.");
          setLoading(false);
        }
      })();
      initialLoadRef.current = request;
      try { await request; }
      finally { if (initialLoadRef.current === request) initialLoadRef.current = null; }
    };
    void check();
    return () => { active = false; if (retryTimer !== undefined) clearTimeout(retryTimer); };
  }, [adoptResponse, loadAttempt]);

  // Hotplug pushes the driver status from the backend, so the dot is right even
  // while the panel is shut. Registered unconditionally: a controller is plugged
  // in with the Quick Access Menu closed far more often than with it open, and
  // adopting the state on arrival beats discovering it on the next open.
  //
  // Only the status. A hotplug re-applies the stored profile without changing
  // it, so a settings payload would carry nothing new - and feeding one to
  // adoptResponse bumps editSeq, which would make the panel throw away the
  // reply to an edit the user was making at that moment.
  useEffect(() => {
    const onDevice = (status: DriverStatus) => {
      driverRevisionRef.current += 1;
      setDriver(status);
    };
    addEventListener<[DriverStatus]>("device", onDevice);
    return () => removeEventListener<[DriverStatus]>("device", onDevice);
  }, []);

  // Still re-checked on open, as the backstop for the cases no event covers:
  // without pyudev there is no hotplug monitor at all, and a plugin reload
  // starts with whatever the hardware already is.
  useEffect(() => {
    if (loading || loadError || setupErr) return;
    const initialRead = initialDriverReadRef.current;
    initialDriverReadRef.current = false;
    if (visible && !initialRead) void refreshDriver();
  }, [visible, loading, loadError, setupErr, refreshDriver]);

  // Game changes are applied by the backend; just adopt what it reports.
  useEffect(() => AppWatcher.listen(adoptResponse), [adoptResponse]);

  // Handlers

  const applyField = useCallback(
    async (key: string, optimistic: Partial<VibeSettings>, call: (app: string, profile: string) => Promise<ApplyResponse>) => {
      if (!mounted.current || controlActionPending.current) return;
      vibeReadEpoch += 1;
      const seq = ++editSeq.current;
      const revision = gameRevision.current;
      const expectedApp = appId;
      const expectedProfile = perGameOn ? appId : DEFAULT_APP;
      const fieldScope = JSON.stringify([expectedApp, expectedProfile, key]);
      latestFieldEdit.current[fieldScope] = seq;
      setSettings((prev) => ({ ...prev, ...optimistic }));
      debounce(key, () => {
        const isCurrent = () => mounted.current && revision === gameRevision.current && seq === editSeq.current;
        const request = enqueueMutation(async () => {
          // Debouncing alone cannot coalesce gestures already waiting behind a
          // slow write. Keep the latest accepted value for each field/profile,
          // including when closing the page flushes its final gesture.
          if (latestFieldEdit.current[fieldScope] !== seq) return;
          return call(expectedApp, expectedProfile);
        });
        request.then((res) => {
            if (!res || !isCurrent()) return;
            checkResult("Could not apply setting", res);
            if (res.app_id !== undefined && res.profile_id !== undefined && res.overwrite !== undefined)
              adoptResponse(res as SettingsResponse);
            else if (res.settings) setSettings(res.settings);
          })
          .catch((e) => {
            if (!isCurrent()) return;
            notifyFailure("Could not apply setting", e);
            // Only resync when nothing newer is pending; the newer edit's own
            // reply is the one that should decide what the panel shows.
            void getSettings().then(res => { if (isCurrent()) adoptResponse(res); }).catch(() => undefined);
          });
      });
    },
    [debounce, adoptResponse, enqueueMutation, appId, perGameOn],
  );

  const handleLevel = useCallback(
    (val: number) => void applyField("level", { level: val }, (app, profile) => setIntensity(val, app, profile)),
    [applyField],
  );

  const handleMode = useCallback(
    // No sample is played here on purpose: the controller firmware already
    // demonstrates the new pattern. Adding our own put a second rumble on
    // top of it, which is why the same mode felt different every time.
    (val: number) => void applyField("mode", { mode: val }, (app, profile) => setRumbleMode(val, app, profile)),
    [applyField],
  );

  const handleTpIntensity = useCallback(
    (val: number) =>
      void applyField("tpIntensity", { touchpadIntensity: val }, (app, profile) => setTouchpadIntensity(val, app, profile)),
    [applyField],
  );

  const handleTpToggle = useCallback(
    (val: boolean) =>
      void applyField("tpEnabled", { touchpadEnabled: val }, (app, profile) => setTouchpadEnabled(val, app, profile)),
    [applyField],
  );

  const handleReset = useCallback(async () => {
    if (!mounted.current || controlActionPending.current) return;
    controlActionPending.current = true;
    setProfileChanging(true);
    const expectedApp = appId, expectedProfile = perGameOn ? appId : DEFAULT_APP;
    const revision = gameRevision.current;
    flushPendingWrites();
    const seq = ++editSeq.current;
    const isCurrent = () => mounted.current && revision === gameRevision.current && seq === editSeq.current;
    try {
      const res = await enqueueMutation(() => resetToDefault(expectedApp, expectedProfile));
      if (!isCurrent()) return;
      // Reset commits its profile before applying hardware. A failed apply
      // still carries the authoritative saved settings and profile context.
      if (res.app_id !== undefined && res.profile_id !== undefined && res.overwrite !== undefined)
        adoptResponse(res as SettingsResponse);
      else if (res.settings) setSettings(res.settings);
      if (checkResult("Reset failed", res)) {
        notify("LeGo Vibe Control", "Settings restored to defaults");
      }
    } catch (e) {
      if (isCurrent()) notifyFailure("Reset failed", e);
    } finally {
      controlActionPending.current = false;
      if (mounted.current) setProfileChanging(false);
    }
  }, [appId, perGameOn, enqueueMutation, flushPendingWrites, adoptResponse]);

  const handleTest = useCallback(async () => {
    if (!mounted.current || testPending.current || controlActionPending.current) return;
    testPending.current = true;
    setTesting(true);
    const revision = gameRevision.current;
    try {
      // A slider value is optimistic until its debounced save completes.
      // Test the confirmed value from this gesture, rather than the old one.
      flushPendingWrites();
      await vibeMutationChain;
      if (!mounted.current || revision !== gameRevision.current) return;
      const res = await testVibration(500);
      checkResult("Test vibration failed", res);
    } catch (e) {
      notifyFailure("Test vibration failed", e);
    } finally {
      testPending.current = false;
      if (mounted.current) setTesting(false);
    }
  }, [flushPendingWrites]);

  const handlePerGameToggle = useCallback(
    async (val: boolean) => {
      if (!mounted.current || controlActionPending.current) return;
      controlActionPending.current = true;
      setProfileChanging(true);
      const revision = gameRevision.current;
      flushPendingWrites();
      ++editSeq.current;
      const isCurrent = () => mounted.current && revision === gameRevision.current;
      setPerGameOn(val);
      try {
        // The backend applies the resolved profile for us, which is what the
        // old frontend skipped when *enabling* a profile - the UI showed the
        // game's values while the hardware kept the global ones.
        const name = AppWatcher.displayName();
        const res = await enqueueMutation(() => setProfileOverwrite(appId, val, name));
        if (!isCurrent()) return;
        checkResult("Could not switch profile", res);
        if (res.app_id !== undefined && res.profile_id !== undefined && res.overwrite !== undefined)
          adoptResponse(res as SettingsResponse);
        else if (res.success) setSettings(res.settings);
        else setPerGameOn(!val);
      } catch (e) {
        if (isCurrent()) {
          setPerGameOn(!val);
          notifyFailure("Could not switch profile", e);
          try {
            const snapshot = await getSettings();
            if (isCurrent()) adoptResponse(snapshot);
          } catch (readError) {
            if (isCurrent()) setLoadError(readError instanceof Error ? readError.message : "Could not confirm the active vibration profile.");
          }
        }
      } finally {
        controlActionPending.current = false;
        if (mounted.current) setProfileChanging(false);
      }
    },
    [appId, enqueueMutation, flushPendingWrites, adoptResponse],
  );

  // Render

  const retryLoad = () => {
    setLoadError(""); setSetupErr(null); setLoading(true);
    setLoadAttempt(attempt => attempt + 1);
  };

  if (loadError) return <PanelSection title="Vibration settings unavailable">
    <PanelSectionRow><Field focusable label="Could not load settings" description={loadError} /></PanelSectionRow>
    <PanelSectionRow><ButtonItem layout="below" onClick={retryLoad}>Retry settings</ButtonItem></PanelSectionRow>
  </PanelSection>;

  if (setupErr) {
    return (
      <PanelSection title="Setup Error">
        <PanelSectionRow>
          <Focusable><div style={styles.errorBox}>{setupErr}</div></Focusable>
        </PanelSectionRow>
      </PanelSection>
    );
  }

  if (loading) {
    return (
      <PanelSection title="Initializing...">
        <PanelSectionRow>
          <Spinner />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const driverFound = driver?.found ?? false;
  const gameRunning = appId !== DEFAULT_APP;
  const modeName = modes[settings.mode] ?? FALLBACK_MODES[0];

  return (
    <div style={styles.container}>
      <PanelSection title="Current vibration">
        <PanelSectionRow>
          <Field focusable label={`${modeLabel(modeName)} · ${LEVEL_LABELS[settings.level] ?? "Unknown"}`}
            description={`Both controller handles · ${gameRunning && perGameOn ? gameName : "Global profile"}`} />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Per-Game Profile">
        <PanelSectionRow>
          <ToggleField
            label="Per Game Profile"
            description={
              gameRunning ? (
                perGameOn ? (
                  <span style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
                    <span>{gameName}</span>
                    <span>
                      <span style={styles.profileTag}>
                        {modeLabel(modeName)} | {LEVEL_LABELS[settings.level]}
                      </span>
                    </span>
                  </span>
                ) : (
                  gameName
                )
              ) : (
                "Launch a game to use per-game profiles."
              )
            }
            checked={perGameOn && gameRunning}
            disabled={!gameRunning || applying}
            onChange={handlePerGameToggle}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Vibration">
        <PanelSectionRow>
          <SliderField
            label="Intensity"
            disabled={profileChanging}
            description={
              <span>
                Level: <span style={styles.valueTag}>{LEVEL_LABELS[settings.level]}</span>
              </span>
            }
            value={settings.level}
            min={0}
            max={3}
            step={1}
            notchCount={4}
            notchLabels={LEVEL_NOTCHES.map((label, notchIndex) => ({ notchIndex, label }))}
            onChange={handleLevel}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Mode"
            disabled={profileChanging}
            description={
              <span>
                Mode: <span style={styles.valueTag}>{modeLabel(modeName)}</span>
              </span>
            }
            value={settings.mode}
            min={0}
            max={Math.max(0, modes.length - 1)}
            step={1}
            notchCount={modes.length}
            notchLabels={modes.map((raw, notchIndex) => ({ notchIndex, label: modeNotch(raw) }))}
            onChange={handleMode}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Touchpad">
        <PanelSectionRow>
          <ToggleField
            label="Touchpad vibration"
            disabled={profileChanging}
            description="Enable vibration on touchpad"
            checked={settings.touchpadEnabled}
            onChange={handleTpToggle}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Touchpad intensity"
            description={
              <span>
                Level:{" "}
                <span style={styles.valueTag}>{LEVEL_LABELS[settings.touchpadIntensity]}</span>
              </span>
            }
            value={settings.touchpadIntensity}
            min={0}
            max={3}
            step={1}
            notchCount={4}
            notchLabels={LEVEL_NOTCHES.map((label, notchIndex) => ({ notchIndex, label }))}
            disabled={profileChanging || !settings.touchpadEnabled}
            onChange={handleTpIntensity}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Actions">
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            description="Tests current intensity and mode."
            onClick={handleTest}
            disabled={applying || testing}
          >
            {testing ? "Vibrating..." : "Test Vibration (0.5s)"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleReset} disabled={applying || testing}>
            Reset to defaults
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Driver Status">
        <PanelSectionRow>
          <Field focusable label={driverFound ? "Controller driver ready" : "Controller driver unavailable"}
            description={driverFound ? `hid-lenovo-go${driver?.method ? ` · ${driver.method}` : ""}${driver?.ids ? ` · ${driver.ids}` : ""}`
              : "The hid-lenovo-go sysfs endpoint was not detected. Requires SteamOS 3.8+ / Kernel 6.18+ with the hid-lenovo-go module loaded on Legion Go hardware."} />
        </PanelSectionRow>
        {driverFound && <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setShowDriverDetails(value => !value)}>
            {showDriverDetails ? "Hide driver details" : "Show driver details"}
          </ButtonItem>
        </PanelSectionRow>}
        {showDriverDetails && driverFound && <PanelSectionRow>
          <Field focusable label="Controller endpoints" description={
            <span style={{ display: "block", maxWidth: "100%", overflowWrap: "anywhere" }}>
              {driver?.paths?.join(" · ") || "No endpoint reported"}
            </span>
          } />
        </PanelSectionRow>}
      </PanelSection>

      <PanelSection title="Notes">
        <PanelSectionRow>
          <Focusable><div style={styles.infoBox}>
            Intensity levels: Off, Low, Medium, High. Mode selects the vibration pattern, applied to
            both handles. Settings persist across reboots and are re-applied after sleep or a
            controller reconnect. Per-game profiles auto-apply when a game with a saved profile
            starts.
          </div></Focusable>
        </PanelSectionRow>
      </PanelSection>
    </div>
  );
};

// ── Plugin entry point ────────────────────────────────────────────────────────

export const VibrationPage = LGoVibeControl;
export const getVibrationSettings = async () => {
  if (vibeMutationsPending) await vibeMutationChain;
  return getSettings();
};
export const startVibrationWatcher = () => AppWatcher.start();
export const stopVibrationWatcher = () => AppWatcher.stop();
