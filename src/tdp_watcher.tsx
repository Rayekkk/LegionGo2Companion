// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP
// TDP page: the foreground game reported to the backend.

import { runningApp, watchRunningApp, type RunningApp } from "./shared";
import { type RunningGame, reapply, setActiveApp } from "./tdp_api";

// ── Running app watcher ────────────────────────────────────────────────────────

export type GameListener = (game: RunningGame | null) => void;
export const asGame = (app: RunningApp | null): RunningGame | null =>
  app ? { appId: String(app.appid), name: app.display_name ?? String(app.appid) } : null;

// The backend trusts a frontend-reported appid for 12 seconds. Refresh at half
// that, so one dropped call is not enough to make it fall back to the /proc scan.
export const PUSH_INTERVAL_MS = 6000;

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
export class AppWatcher {
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

