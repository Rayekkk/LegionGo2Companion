// SPDX-License-Identifier: BSD-3-Clause
// Helpers shared by the Decky pages: D2 calls, edit queues and the running app watcher (render helpers are in ui.tsx).
import { callable } from "@decky/api";
import { findModuleExport, Router } from "@decky/ui";
import { settle, type Result } from "./result";

/**
 * callable() through the backend's D2 answers ({ok, error?, state?}): the state, also of a refusal that carries one
 * (a status read whose error stays on screen, e.g. battery or WiFi); any other refusal or failed call is thrown as an
 * Error with its text, as a rejected call was.
 */
export const rpc = <A extends unknown[], T>(name: string) => {
  const call = callable<A, Result<T>>(name);
  return (...args: A): Promise<T> => settle(call(...args)).then((answer) => {
    if (answer.ok || "state" in answer) return answer.state as T;
    throw new Error(answer.error || "Unknown error");
  });
};

/** An Error's message; anything else as the fallback, or as text when there is none. */
export const errorText = (error: unknown, fallback?: string) =>
  error instanceof Error ? error.message : fallback ?? String(error);

/**
 * Accepted edits outlive the page that made them. Each module keeps one queue;
 * later snapshot reads wait for it with `if (queue.pending) await queue.tail`
 * (synchronous when idle). An operation calls `queue.done()` once it settles.
 */
export const mutationQueue = () => {
  const queue = {
    tail: Promise.resolve() as Promise<unknown>,
    pending: 0,
    push<T>(run: () => Promise<T>): Promise<T> {
      queue.pending += 1;
      const queued = queue.tail.then(run, run);
      queue.tail = queued.catch(() => undefined);
      return queued;
    },
    done() { queue.pending -= 1; },
  };
  return queue;
};

// ── Running app watcher ───────────────────────────────────────────────────────

export type RunningApp = { appid: string | number; display_name?: string };
/** A module fed by the game watcher; it keeps its own state, gating and RPC. */
type GameClient = { check(app: RunningApp | null): Promise<void>; resume(): void };

const gameClients = new Set<GameClient>();
let gameGeneration = 0;
let gameTimer: ReturnType<typeof setInterval> | undefined;
let gameUnsubs: Array<() => void> = [];
const lateChecks = new Set<ReturnType<typeof setTimeout>>();

/** Router.MainRunningApp, or null when nothing runs or the router is unavailable. */
export const runningApp = (): RunningApp | null => {
  try {
    const app = Router?.MainRunningApp;
    return app?.appid ? app : null;
  } catch {
    return null;
  }
};

/** One Router read, handed to every watching module. */
const checkRunningApp = () => {
  const app = runningApp();
  return Promise.all([...gameClients].map((client) => client.check(app)));
};

/**
 * Subscribe to resume-from-suspend. Returns an unsubscribe function, or null
 * when the client offers no way to hear about it.
 *
 * `SteamClient.System.RegisterForOnResumeFromSuspend` was removed from the
 * Steam client in the September 2025 beta. Optional chaining meant calling it
 * silently did nothing - confirmed on the device, where two suspend cycles
 * produced no callback and no error. The replacement lives on a SleepManager
 * module, reachable either as a global or through the webpack exports; the
 * legacy call stays as a fallback for older clients.
 */
/** Steam's registration as an unsubscribe function: it returns an object with unregister() or a plain function. */
const asUnsub = (reg: SteamUnregister | undefined): (() => void) | null => {
  if (typeof reg === "function") return reg;
  if (typeof reg?.unregister === "function") return () => reg.unregister();
  return null;
};

function onResumeFromSuspend(handler: () => void): (() => void) | null {
  const isSleepManager = (e: unknown) => {
    const candidate = e as SteamSleepManager | null;
    return !!candidate && typeof candidate === "object" &&
      (typeof candidate.RegisterForNotifyResumeFromSuspend === "function" ||
        typeof candidate.NotifyResumeFromSuspend === "function");
  };

  try {
    const mgr: SteamSleepManager | undefined = window.SleepManager ?? findModuleExport(isSleepManager);
    const unsub = asUnsub(mgr?.RegisterForNotifyResumeFromSuspend?.(handler));
    if (unsub) return unsub;
  } catch (e) {
    console.warn("[legiongo2companion] game_watcher.resume_lookup_failed");
  }

  try {
    const unsub = asUnsub(
      window.SteamClient?.System?.RegisterForOnResumeFromSuspend?.(handler));
    if (unsub) return unsub;
  } catch (e) {
    console.warn("[legiongo2companion] game_watcher.legacy_resume_registration_failed");
  }

  // Said out loud rather than swallowed: this going quiet again is exactly how
  // the previous registration rotted unnoticed.
  console.warn("[legiongo2companion] no resume-from-suspend notification available; "
    + "TDP and vibration will be restored by the backend loops instead");
  return null;
}

function startGameWatch() {
  const generation = ++gameGeneration;
  const live = () => generation === gameGeneration;
  const steam = window.SteamClient;
  try {
    const reg = steam?.GameSessions?.RegisterForAppLifetimeNotifications?.(() => {
      if (!live()) return;
      // Router.MainRunningApp lags the notification slightly.
      const timer = setTimeout(() => {
        lateChecks.delete(timer);
        if (live()) void checkRunningApp();
      }, 300);
      lateChecks.add(timer);
    });
    const unsub = asUnsub(reg);
    if (unsub) gameUnsubs.push(unsub);
  } catch (e) {
    console.warn("[legiongo2companion] game_watcher.app_notifications_unavailable");
  }
  // Decky has no backend resume hook, so this notification is the only way to
  // restore SMU limits and controller settings before the backend loops notice.
  const offResume = onResumeFromSuspend(() => {
    if (live()) gameClients.forEach((client) => client.resume());
  });
  if (offResume) gameUnsubs.push(offResume);
  // Router.MainRunningApp can change without a lifetime event firing.
  gameTimer = setInterval(() => void checkRunningApp(), 2000);
}

function stopGameWatch() {
  gameGeneration += 1;
  for (const timer of lateChecks) clearTimeout(timer);
  lateChecks.clear();
  if (gameTimer) clearInterval(gameTimer);
  gameTimer = undefined;
  for (const off of gameUnsubs) {
    try {
      off();
    } catch {
      /* the subscription may already be gone */
    }
  }
  gameUnsubs = [];
}

/**
 * Feed `client` the foreground app: now, every 2 s and after app lifetime
 * events, plus resume-from-suspend. One timer and one set of Steam
 * subscriptions serve every module. Returns the unsubscribe.
 */
export function watchRunningApp(client: GameClient): () => void {
  if (!gameClients.size) startGameWatch();
  gameClients.add(client);
  void client.check(runningApp());
  return () => {
    if (gameClients.delete(client) && !gameClients.size) stopGameWatch();
  };
}
