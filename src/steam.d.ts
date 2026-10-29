// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/frontend/steam.d.ts): change it there, then `kit sync`.
//
// Steam client globals the plugins use, typed instead of `any`. Sources: DeckyLS (appStore, URL),
// FSR4Injector/DeckyReshade (Apps, GameSessions, store_category), LegionGo2Companion/Ayaneo3Companion (resume from
// suspend through SleepManager, unsubscribe as an object or a function). Guarantees: every member a plugin calls is
// declared with its arguments; nothing is `any`. Members are optional where Steam builds lack them, so callers must
// check. Add a member here (in DeckyKit) when a plugin needs one; never redeclare these globals in a plugin.

interface SteamAppOverview {
  appid: number;
  display_name: string;
  /** 1 game, 2 application, 4 tool, 8 demo, 0x40000000 non-Steam shortcut. */
  app_type: number;
  store_category?: number[];
}

interface SteamAppDetails {
  strLaunchOptions?: string;
  [key: string]: unknown;
}

interface SteamAppLifetimeNotification {
  unAppID: number;
  nInstanceID: number;
  bRunning: boolean;
}

/** Steam's registrations return an object with unregister(), some newer ones a plain function. */
type SteamUnregister = { unregister(): void } | (() => void);

/** Found through @decky/ui's findModuleExport (not a global); the way to hear about resume since Steam's 2025 betas. */
interface SteamSleepManager {
  RegisterForNotifyResumeFromSuspend?(callback: () => void): SteamUnregister;
  RegisterForNotifyRequestSuspend?(callback: () => void): SteamUnregister;
  /** Only used to recognise the module among webpack exports (LegionGo2Companion). */
  NotifyResumeFromSuspend?(...args: unknown[]): unknown;
}

declare const appStore: {
  GetAppOverviewByAppID(appid: number): SteamAppOverview | null | undefined;
};

declare const SteamClient: {
  URL: { ExecuteSteamURL(url: string): void };
  Apps: {
    RegisterForAppDetails(appid: number, callback: (details: SteamAppDetails) => void): SteamUnregister;
    SetAppLaunchOptions(appid: number, options: string): void;
  };
  GameSessions: {
    RegisterForAppLifetimeNotifications(callback: (n: SteamAppLifetimeNotification) => void): SteamUnregister;
  };
  System?: {
    /** @deprecated Gone from Steam's September 2025 beta (fails silently): use SteamSleepManager, keep this as a fallback. */
    RegisterForOnResumeFromSuspend?(callback: () => void): SteamUnregister;
  };
};

/** The same globals read through window, where a test host or an older Steam build may lack them. */
interface Window {
  SleepManager?: SteamSleepManager;
  SteamClient?: typeof SteamClient;
}
