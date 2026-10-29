// SPDX-License-Identifier: BSD-3-Clause AND MIT
// Copyright (c) 2026 Rayekkk
// Portions copyright (c) 2026 piyush-tyagi-13 and M4ttiA, MIT - see LICENSE.MIT
// https://github.com/Rayekkk/LeGo-Vibe-Control
// Vibration page: backend calls, their types, toasts and the foreground game watcher.

import { callable, toaster } from "@decky/api";
import { useCallback, useEffect, useRef } from "react";
import { errorText, rpc, runningApp, watchRunningApp, type RunningApp } from "./shared";
import { settle, type Result } from "./result";
import { failureText } from "../desktop/native/vibration.mjs";

// ── Types ─────────────────────────────────────────────────────────────────────

export interface VibeSettings {
  level: number;
  mode: number;
  touchpadIntensity: number;
  touchpadEnabled: boolean;
}

export interface SettingsResponse {
  settings: VibeSettings;
  configured?: boolean;
  app_id: string;
  profile_id: string;
  overwrite: boolean;
}

export interface ApplyResponse {
  ok: boolean;
  settings: VibeSettings;
  error?: string;
  app_id?: string;
  profile_id?: string;
  overwrite?: boolean;
}

export interface DriverStatus {
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

export const DEFAULT_APP = "0";
const APP_HEARTBEAT_MS = 6000;

// ── Backend callables ─────────────────────────────────────────────────────────

type ChangeResult = Result<Omit<ApplyResponse, "ok" | "error">>;

/**
 * A change's D2 answer as the page reads it: its state with ok and the error. A refusal without a state (a module
 * guard, a failed call) is thrown with its text, as a rejected call was.
 */
const change = <A extends unknown[]>(call: (...args: A) => Promise<ChangeResult>) =>
  (...args: A) => settle(call(...args)).then((answer): ApplyResponse => {
    if (answer.ok || "state" in answer) return { ...answer.state!, ok: answer.ok, error: answer.error };
    throw new Error(answer.error || "Unknown error");
  });

export const isReady = rpc<[], ReadyState>("vibe_is_ready");
export const getSettings = rpc<[], SettingsResponse>("vibe_get_settings");
export const setActiveApp = rpc<[string], SettingsResponse & { changed: boolean }>("vibe_set_active_app");

export const setIntensity = change(callable<[number, string, string], ChangeResult>("vibe_set_intensity"));
export const setRumbleMode = change(callable<[number, string, string], ChangeResult>("vibe_set_rumble_mode"));
export const setTouchpadIntensity = change(callable<[number, string, string], ChangeResult>("vibe_set_touchpad_intensity"));
export const setTouchpadEnabled = change(callable<[boolean, string, string], ChangeResult>("vibe_set_touchpad_enabled"));
export const resetToDefault = change(callable<[string, string], ChangeResult>("vibe_reset_to_default"));
export const reapply = change(callable<[], ChangeResult>("vibe_reapply"));

export const setProfileOverwrite = change(callable<[string, boolean, string], ChangeResult>("vibe_set_profile_overwrite"));

export const getDriverStatus = rpc<[], DriverStatus>("vibe_get_driver_status");
export const getCapabilities = rpc<[], { intensity: string[]; mode: string[]; tp_intensity: string[] }>("vibe_get_capabilities");
export const testVibration = rpc<[number], void>("vibe_test_vibration");

// ── Toasts ────────────────────────────────────────────────────────────────────

// Toasts carry the module name; a failure's body says what failed and why.
export const notify = (body: string) => {
  try {
    toaster.toast({ title: "Vibration", body, duration: 4000 });
  } catch {
    console.error("[legiongo2companion] vibration.notification_failed");
  }
};

export const notifyFailure = (what: string, err: unknown) => {
  console.error("[legiongo2companion] vibration.operation_failed");
  notify(`${what}: ${errorText(err ?? "Unknown error")}`);
};

/** Report a backend result that carries its own error string. */
export const checkResult = (what: string, res: { ok: boolean; error?: string }) => {
  if (!res.ok) notify(failureText(what, res.error));
  return res.ok;
};

// ── Running app watcher ───────────────────────────────────────────────────────

type SettingsListener = (res: SettingsResponse, readEpoch?: number) => void;
/** Bumped around every edit, so a game-watcher snapshot read before it cannot overwrite it. */
export const vibeReads = { epoch: 0 };
const appIdOf = (app: RunningApp | null) => String(app?.appid || DEFAULT_APP);

/**
 * Tracks the foreground game and tells the backend about it, so the backend
 * can resolve per-game profiles on its own - including for hotplug and
 * resume, which never go through the UI. Fed by the shared watchRunningApp.
 */
export class AppWatcher {
  private static listeners: SettingsListener[] = [];
  private static currentId = DEFAULT_APP;
  private static unwatch: (() => void) | undefined;
  private static started = false;
  private static busy = false;
  private static lastPush = 0;
  private static reportFailed = false;
  private static generation = 0;

  static displayName(): string {
    const app = runningApp();
    return app ? (app.display_name || `App ${app.appid}`) : "";
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
    this.generation += 1;
    this.currentId = appIdOf(runningApp());
    this.unwatch = watchRunningApp(this);
  }

  static stop() {
    this.unwatch?.();
    this.unwatch = undefined;
    this.started = false;
    this.generation += 1;
    this.busy = false;
    this.listeners = [];
    this.currentId = DEFAULT_APP;
    this.lastPush = 0;
    this.reportFailed = false;
  }

  // The controller comes back at its firmware defaults, and the backend's
  // write cache would otherwise skip the rewrite.
  static resume() {
    if (!this.started) return;
    void reapply()
      .then((res) => {
        if (!res.ok) console.warn("[legiongo2companion] vibration.resume_reapply_failed");
      })
      .catch(() => console.error("[legiongo2companion] vibration.resume_reapply_failed"));
  }

  static async check(app = runningApp()) {
    if (!this.started || this.busy) return;
    const generation = this.generation;
    const id = appIdOf(app);
    const changed = id !== this.currentId;
    const now = Date.now();
    if (!changed && now - this.lastPush < APP_HEARTBEAT_MS) return;
    this.busy = true;
    const readEpoch = vibeReads.epoch;
    try {
      const res = await setActiveApp(id);
      if (!this.started || generation !== this.generation) return;
      if (appIdOf(runningApp()) !== id) return;
      if (this.reportFailed) console.info("[legiongo2companion] vibration.foreground_reporting_restored");
      this.reportFailed = false;
      // Committed only once the backend has it. Recording the id before the
      // call meant a single failed RPC - the loader restarting, say - left
      // every later tick thinking there was nothing to send, so the hardware
      // kept the previous game's profile for the rest of the session.
      this.currentId = id;
      this.lastPush = now;
      this.listeners.forEach((fn) => fn(res, readEpoch));
    } catch (e) {
      if (this.started && generation === this.generation && !this.reportFailed) {
        this.reportFailed = true;
        console.warn("[legiongo2companion] vibration.foreground_reporting_unavailable");
      }
    } finally {
      if (generation === this.generation) {
        this.busy = false;
        // A game changed while the previous RPC was busy. Deliver its profile
        // immediately instead of waiting for the next two-second timer tick.
        if (this.started && appIdOf(runningApp()) !== id) void this.check();
      }
    }
  }
}

// ── Slider writes ─────────────────────────────────────────────────────────────

// Coalesces a slider drag into a single backend call. The UI still moves
// immediately; only the RPC and its disk commit are deferred.
export const useDebouncedWrites = () => {
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
  return { debounce, flushPendingWrites, pendingWrites };
};
