// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP

import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  toaster,
  useQuickAccessVisible,
} from "@decky/api";
import {
  ButtonItem,
  Field,
  findModuleExport,
  PanelSection,
  PanelSectionRow,
  Router,
  SliderField,
  Spinner,
  staticClasses,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";

// ── Helpers ────────────────────────────────────────────────────────────────────
const toMw  = (w: number)  => w * 1000;
const toW   = (mw: number) => Math.round(mw / 1000);
const fmt   = (v?: number) => v != null ? `${v.toFixed(1)} W` : "-";
const clamp = (v: number, lo: number, hi: number) => Math.min(Math.max(v, lo), hi);

// ── Tuning model ───────────────────────────────────────────────────────────────
// SPL is the actual TDP dial. SPPT and FPPT are expressed as headroom *above* SPL
// rather than absolute watts, so raising the TDP carries the burst limits with it.
interface Tuning { spl: number; spptOff: number; fpptOff: number }
interface Caps   { spl: number; sppt: number; fppt: number }

const OFFSET_MAX = { sppt: 10, fppt: 15 };

// Used until get_caps() answers; the backend reports the firmware's real ceilings
// and falls back to these same numbers (FALLBACK_STD_W in main.py) if it cannot.
const FALLBACK_STD: Caps = { spl: 35, sppt: 37, fppt: 45 };
const FALLBACK_MAX: Caps = { spl: 50, sppt: 50, fppt: 50 };
const FALLBACK_MIN = 5;

/** Headroom still available above the current SPL, per parameter. */
const offsetMax = (spl: number, caps: Caps) => ({
  sppt: Math.max(0, Math.min(OFFSET_MAX.sppt, caps.sppt - spl)),
  fppt: Math.max(0, Math.min(OFFSET_MAX.fppt, caps.fppt - spl)),
});

/** Force a tuning back inside the ceilings, keeping SPPT <= FPPT. */
function normalise(t: Tuning, caps: Caps, minW: number): Tuning {
  const spl = clamp(t.spl, minW, caps.spl);
  const max = offsetMax(spl, caps);
  const spptOff = clamp(t.spptOff, 0, max.sppt);
  const fpptOff = Math.max(clamp(t.fpptOff, 0, max.fppt), spptOff);
  return { spl, spptOff, fpptOff };
}

const absolute = (t: Tuning) => ({
  spl: t.spl, sppt: t.spl + t.spptOff, fppt: t.spl + t.fpptOff,
});

const fromAbsolute = (spl: number, sppt: number, fppt: number): Tuning => ({
  spl, spptOff: Math.max(0, sppt - spl), fpptOff: Math.max(0, fppt - spl),
});

/** Slider handlers implementing the coupling rules between the three limits. */
function makeTuningHandlers(t: Tuning, set: (next: Tuning) => void, caps: Caps, minW: number) {
  return {
    // Moving SPL re-clamps both offsets: at the ceiling there is no headroom left.
    onSpl: (v: number) => set(normalise({ ...t, spl: v }, caps, minW)),
    onSppt: (v: number) => {
      const spptOff = clamp(v, 0, offsetMax(t.spl, caps).sppt);
      // Pushing SPPT up drags FPPT along so SPPT never overtakes it.
      set({ ...t, spptOff, fpptOff: Math.max(t.fpptOff, spptOff) });
    },
    onFppt: (v: number) => {
      const fpptOff = clamp(v, 0, offsetMax(t.spl, caps).fppt);
      // Pulling FPPT below SPPT drags SPPT down to meet it.
      set({ ...t, fpptOff, spptOff: Math.min(t.spptOff, fpptOff) });
    },
  };
}

// ── Presets ────────────────────────────────────────────────────────────────────
type PresetKey = "minimum" | "silent" | "balanced" | "performance" | "max" | "custom";

type PresetTable = Record<Exclude<PresetKey, "custom">,
                          { spl: number; sppt: number; fppt: number }>;

// Used until get_caps() answers with the ladder for this machine. The backend
// is the source of truth, because it is the side that knows the hardware.
const PRESETS: PresetTable = {
  minimum:     { spl: 5,  sppt: 5,  fppt: 10 },
  silent:      { spl: 8,  sppt: 10, fppt: 15 },
  balanced:    { spl: 15, sppt: 18, fppt: 25 },
  performance: { spl: 25, sppt: 28, fppt: 35 },
  max:         { spl: 35, sppt: 37, fppt: 45 },
};

const PRESET_LABELS: Record<PresetKey, string> = {
  minimum:     "Minimum",
  silent:      "Silent",
  balanced:    "Balanced",
  performance: "Performance",
  max:         "Max",
  custom:      "Custom",
};

const PRESET_ORDER: PresetKey[] = ["minimum", "silent", "balanced", "performance", "max", "custom"];

function detectPreset(spl: number, sppt: number, fppt: number,
                      table: PresetTable = PRESETS): PresetKey {
  for (const key of Object.keys(table) as Exclude<PresetKey, "custom">[]) {
    const v = table[key];
    if (v.spl === spl && v.sppt === sppt && v.fppt === fppt) return key;
  }
  return "custom";
}

function profileLabel(spl: number, sppt: number, fppt: number, stored?: string,
                      table: PresetTable = PRESETS): string {
  const customLabel = `Custom (${spl} +${sppt - spl}/+${fppt - spl})`;
  if (stored !== undefined) {
    if (stored === "custom" || stored === "") return customLabel;
    return PRESET_LABELS[stored as PresetKey] ?? stored;
  }
  const key = detectPreset(spl, sppt, fppt, table);
  return key === "custom" ? customLabel : PRESET_LABELS[key];
}

const exceedsCaps = (spl: number, sppt: number, fppt: number, caps: Caps) =>
  spl > caps.spl || sppt > caps.sppt || fppt > caps.fppt;

function statusStyle(msg: string) {
  return msg.startsWith("Error") ? styles.errorBox : { fontSize: "12px", color: OK_COLOR };
}

// ── Types ──────────────────────────────────────────────────────────────────────
interface Settings   { spl: number; sppt: number; fppt: number; enabled: boolean; active_preset?: string }
interface TdpResult  { success: boolean; stderr?: string; skipped?: boolean; returncode?: number }
interface TdpValues  {
  spl_limit?:  number;
  sppt_limit?: number;
  fppt_limit?: number;
  package_draw?: number;
  source?: string;
}
interface TdpInfo     { success: boolean; values: TdpValues; error?: string }
interface PowerSource { ac: boolean }
interface GameProfile {
  exists: boolean;
  profile: { spl: number; sppt: number; fppt: number; preset?: string };
  ac_separate: boolean;
  ac_profile: { spl: number; sppt: number; fppt: number; ac_preset?: string };
}
interface CapsInfo   { min: number; std: Caps; max: Caps; wmi: boolean; extras?: boolean;
                       presets?: PresetTable }
interface RunningGame { appId: string; name: string }
interface ReadyState  { ready: boolean; error: string }
interface CpuBoostControl {
  available: boolean;
  enabled: boolean | null;
  error: string;
}

interface EppControl {
  compatibility_note?: string;
  available: boolean;
  value: string | null;
  profiles: string[];
  numeric_supported: boolean;
  numeric_value: number | null;
  min: number;
  max: number;
  error: string;
}

/** Shared payload for the CPU power controls getter and both setters. */
interface CpuPowerControls {
  success: boolean;
  available: boolean;
  cpu_boost: CpuBoostControl;
  epp: EppControl;
  error: string;
  profile?: {
    app_id: string;
    ac_profile: boolean;
    active: boolean;
    cpu_boost_enabled: boolean | null;
    epp: string | null;
  };
}

// ── Backend callables ──────────────────────────────────────────────────────────
const isReady           = callable<[], ReadyState>("is_ready");
const getSettings       = callable<[], Settings>("get_settings");
const getCaps           = callable<[], CapsInfo>("get_caps");
const applyTdp          = callable<[number, number, number, string, string, string | null], TdpResult>("apply_tdp");
const getTdpInfo        = callable<[], TdpInfo>("get_tdp_info");
const getGameProfile    = callable<[string], GameProfile>("get_game_profile");
const deleteGameProfile = callable<[string], TdpResult>("delete_game_profile");
const setPluginEnabled  = callable<[boolean], TdpResult>("set_plugin_enabled");
const setPanelActive    = callable<[boolean], void>("set_panel_active");
const setActiveApp      = callable<[string], void>("set_active_app");
const getPowerSource    = callable<[], PowerSource>("get_power_source");
const reapply           = callable<[], TdpResult>("reapply");
const setGameAcProfile  = callable<[string, number, number, number, boolean, string], TdpResult>("set_game_ac_profile");
const retryExtras = callable<[], { success: boolean; error?: string }>("retry_extras");
const getExtrasUnlocked = callable<[], boolean>("get_extras_unlocked");
const setExtrasUnlockedCall = callable<[boolean], TdpResult>("set_extras_unlocked");
const getCpuPowerControls = callable<[string, boolean], CpuPowerControls>("get_cpu_power_controls");
const setCpuBoost       = callable<[boolean, string, boolean, string], CpuPowerControls>("set_cpu_boost");
const setEpp            = callable<[string, string, boolean, string], CpuPowerControls>("set_epp");

// ── Toasts ─────────────────────────────────────────────────────────────────────

const notify = (title: string, body: string) => {
  try {
    toaster.toast({ title, body, duration: 4000 });
  } catch {
    console.error(`[legotdp] ${title}: ${body}`);
  }
};

const notifyFailure = (title: string, err: unknown) => {
  const body = err instanceof Error ? err.message : String(err ?? "Unknown error");
  console.error(`[legotdp] ${title}`, err);
  notify(title, body);
};

// ── Styles - Steam theme variables with hardcoded fallbacks ────────────────────

const OK_COLOR = "var(--gpColor-Green, #4ade80)";
const BAD_COLOR = "var(--gpColor-Red, #f87171)";
const WARN_COLOR = "var(--gpColor-Yellow, #fbbf24)";
const DIM_COLOR = "var(--gpColor-TextMuted, rgba(255,255,255,0.5))";

const styles = {
  valueTag: {
    fontSize: "13px",
    fontWeight: "bold",
    color: "var(--gpColor-White, #fff)",
    background: "rgba(255,255,255,0.1)",
    borderRadius: "4px",
    padding: "1px 6px",
    fontFamily: "monospace",
  },
  profileTag: {
    fontSize: "11px",
    fontWeight: "bold",
    color: "var(--gpColor-White, #fff)",
    background: "rgba(74,222,128,0.25)",
    border: "1px solid rgba(74,222,128,0.5)",
    borderRadius: "3px",
    padding: "0px 5px",
    fontFamily: "monospace",
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
};

// ── Resume from suspend ────────────────────────────────────────────────────────

/**
 * Subscribe to resume-from-suspend. Returns an unsubscribe function, or null
 * when the client offers no way to hear about it.
 *
 * `SteamClient.System.RegisterForOnResumeFromSuspend` was removed from the
 * Steam client in the September 2025 beta. Optional chaining meant calling it
 * silently did nothing - confirmed on the device, where two suspend cycles
 * produced no callback and no error, and the limits only came back five
 * seconds later when the enforce loop noticed. The replacement lives on a
 * SleepManager module, reachable either as a global or through the webpack
 * exports; the legacy call stays as a fallback for older clients.
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
    console.warn("[legotdp] SleepManager lookup failed", e);
  }

  try {
    const unsub = asUnsub(
      (window as any).SteamClient?.System?.RegisterForOnResumeFromSuspend?.(handler));
    if (unsub) return unsub;
  } catch (e) {
    console.warn("[legotdp] legacy resume registration failed", e);
  }

  // Said out loud rather than swallowed: this going quiet again is exactly how
  // the previous registration rotted unnoticed.
  console.warn("[legotdp] no resume-from-suspend notification available; "
    + "limits will be restored by the enforce loop instead");
  return null;
}

// ── Running app watcher ────────────────────────────────────────────────────────

type GameListener = (game: RunningGame | null) => void;

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
  private static timer: ReturnType<typeof setInterval> | undefined;
  private static unsubs: Array<() => void> = [];
  private static started = false;
  private static busy = false;
  private static lastPush = 0;
  private static generation = 0;
  private static delayedChecks = new Set<ReturnType<typeof setTimeout>>();

  static activeGame(): RunningGame | null {
    try {
      const app = (Router as any)?.MainRunningApp;
      if (!app?.appid) return null;
      return { appId: String(app.appid), name: app.display_name ?? String(app.appid) };
    } catch {
      return null;
    }
  }

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
    const generation = ++this.generation;
    this.current = this.activeGame();

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
      console.warn("[legotdp] app lifetime notifications unavailable", e);
    }

    // The SMU comes back at firmware defaults after sleep. Decky has no backend
    // resume hook - the loader only calls _migration, _main, _unload and
    // _uninstall - so this notification is the only way to beat the enforce
    // loop's five-second tick to it.
    const offResume = onResumeFromSuspend(() => {
      if (!this.started || generation !== this.generation) return;
      void reapply()
        .then((res) => {
          if (!res.success) console.warn("[legotdp] reapply after resume failed", res.stderr);
        })
        .catch((e) => console.error("[legotdp] reapply after resume threw", e));
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
    this.current = null;
    this.started = false;
    this.lastPush = 0;
  }

  private static async check() {
    if (!this.started || this.busy) return;
    const generation = this.generation;
    const game = this.activeGame();
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
        this.lastPush = now;
      } catch (e) {
        console.error("[legotdp] setActiveApp failed", e);
      } finally {
        if (generation === this.generation) this.busy = false;
      }
    }

    if (this.started && generation === this.generation && changed) this.listeners.forEach((fn) => fn(game));
  }
}

// ── Icon ───────────────────────────────────────────────────────────────────────
const ChipIcon: FC = () => (
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"
    style={{ width: "1em", height: "1em" }}>
    <path d="M9 2v2H7a2 2 0 0 0-2 2v2H3v2h2v2H3v2h2v2H3v2h2v2a2 2 0 0 0 2 2h2v2h2v-2h2v2h2v-2h2a2 2 0 0 0 2-2v-2h2v-2h-2v-2h2v-2h-2V9h2V7h-2V6a2 2 0 0 0-2-2h-2V2h-2v2h-2V2H9zm-1 4h12v12H8V6zm3 3v6h6V9h-6z" />
  </svg>
);

// ── Live TDP panel ─────────────────────────────────────────────────────────────

// Must stay comfortably under _PANEL_ACTIVE_TTL_S in main.py (90 s).
const PANEL_LEASE_MS = 30000;

const LivePanel: FC = () => {
  const [info, setInfo] = useState<TdpInfo | null>(null);
  const visible = useQuickAccessVisible();

  // Gated on visibility, not just on mount: the panel stays mounted while the
  // Quick Access Menu is on another tab, and refreshing it there costs a RAPL
  // read every two seconds for nobody to look at. set_panel_active is what
  // gates the backend loop, so nothing is computed while this is unmounted.
  //
  // The backend pushes each refresh rather than answering a poll: it already
  // recomputed these numbers on exactly this cadence, so asking for them over
  // RPC was a round trip to be handed something that already existed.
  //
  // The lease is renewed rather than set once. The cleanup below drops it, but
  // it never runs if the frontend is torn down outright - a Steam UI restart -
  // and the backend would then keep refreshing forever. Thirty seconds against
  // the backend's ninety leaves room for two lost calls, and is still fifteen
  // times less traffic than the two-second poll this replaced.
  useEffect(() => {
    if (!visible) return;
    let active = true;
    setPanelActive(true);
    const lease = setInterval(() => setPanelActive(true), PANEL_LEASE_MS);
    const onInfo = (next: TdpInfo) => { if (active) setInfo(next); };
    addEventListener<[TdpInfo]>("tdp_info", onInfo);
    // Seed it: the first push is a full interval away, and the panel would
    // otherwise show a spinner for two seconds every time it is opened.
    getTdpInfo().then((v) => { if (active) setInfo(v); }).catch(() => undefined);
    return () => {
      active = false;
      clearInterval(lease);
      removeEventListener<[TdpInfo]>("tdp_info", onInfo);
      setPanelActive(false);
    };
  }, [visible]);

  const v = info?.values ?? {};
  return (
    <PanelSection title="Current TDP">
      {!info ? (
        <PanelSectionRow><Spinner /></PanelSectionRow>
      ) : !info.success ? (
        <PanelSectionRow>
          <Field label="Error" description={info.error ?? "Failed to read TDP"} />
        </PanelSectionRow>
      ) : (
        <>
          <PanelSectionRow>
            <Field label="SPL  (Sustained)" description={`Limit: ${fmt(v.spl_limit)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="SPPT (Slow)" description={`Limit: ${fmt(v.sppt_limit)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field label="FPPT (Fast)" description={`Limit: ${fmt(v.fppt_limit)}`} />
          </PanelSectionRow>
          <PanelSectionRow>
            <Field
              label="Package draw"
              description={`${fmt(v.package_draw)}${v.source ? `   -   set via ${v.source}` : ""}`}
            />
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

// ── CPU power controls ────────────────────────────────────────────────────────

const EPP_NAMED_VALUES: Record<string, number> = {
  performance: 0,
  balance_performance: 128,
  balance_power: 191,
  power: 255,
};
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

/** Fail closed instead of rendering a false state from a version-skewed backend. */
function normaliseCpuPowerControls(value: unknown): CpuPowerControls {
  if (!value || typeof value !== "object") {
    throw new Error("Backend returned invalid CPU power controls.");
  }
  const raw = value as Partial<CpuPowerControls>;
  if (typeof raw.success !== "boolean" || typeof raw.available !== "boolean" ||
      typeof raw.error !== "string" || !raw.cpu_boost ||
      typeof raw.cpu_boost !== "object" || !raw.epp || typeof raw.epp !== "object") {
    throw new Error("Backend returned incompatible CPU power controls.");
  }

  const boost = raw.cpu_boost as Partial<CpuBoostControl>;
  const epp = raw.epp as Partial<EppControl>;
  const boostStateValid = typeof boost.enabled === "boolean" || boost.enabled === null;
  const eppValueValid = typeof epp.value === "string" || epp.value === null;
  const numericValueValid = epp.numeric_value === null ||
    (typeof epp.numeric_value === "number" && Number.isInteger(epp.numeric_value) &&
      epp.numeric_value >= 0 && epp.numeric_value <= 255);
  if (typeof boost.available !== "boolean" || !boostStateValid ||
      typeof boost.error !== "string" || typeof epp.available !== "boolean" ||
      !eppValueValid || !Array.isArray(epp.profiles) ||
      epp.profiles.some((profile) => typeof profile !== "string") ||
      typeof epp.numeric_supported !== "boolean" || epp.min !== 0 || epp.max !== 255 ||
      !numericValueValid || typeof epp.error !== "string") {
    throw new Error("Backend returned incompatible CPU power controls.");
  }

  const profile = raw.profile;
  if (profile !== undefined && (!profile || typeof profile !== "object" ||
      typeof profile.app_id !== "string" || typeof profile.ac_profile !== "boolean" ||
      typeof profile.active !== "boolean" ||
      (profile.cpu_boost_enabled !== null && typeof profile.cpu_boost_enabled !== "boolean") ||
      (profile.epp !== null && typeof profile.epp !== "string"))) {
    throw new Error("Backend returned incompatible CPU profile controls.");
  }

  return {
    success: raw.success,
    available: raw.available,
    cpu_boost: {
      available: boost.available,
      enabled: boost.enabled ?? null,
      error: boost.error,
    },
    epp: {
      available: epp.available,
      value: typeof epp.value === "string" && epp.value.trim() ? epp.value.trim() : null,
      profiles: Array.from(new Set(epp.profiles.map((profile) => profile.trim()).filter(Boolean))),
      compatibility_note: typeof epp.compatibility_note === "string" ? epp.compatibility_note : "",
      numeric_supported: epp.numeric_supported,
      numeric_value: epp.numeric_value ?? null,
      min: 0,
      max: 255,
      error: epp.error,
    },
    error: raw.error,
    profile,
  };
}

const readableEppProfile = (profile: string | null) => {
  if (!profile) return "Unknown";
  const readable = profile.replace(/[-_]+/g, " ").trim();
  return readable.charAt(0).toUpperCase() + readable.slice(1);
};

const parseNumericEpp = (value: string | null) => {
  if (!value || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 && parsed <= 255 ? parsed : null;
};

const eppSliderPercent = (epp: EppControl) => {
  const parsed = parseNumericEpp(epp.value);
  const named = epp.value ? EPP_NAMED_VALUES[epp.value] : undefined;
  const rawValue = epp.numeric_value ?? parsed ?? named ?? 128;
  return clamp(Math.round((rawValue * 100 / 255) / 10) * 10, 0, 100);
};

type CpuPowerAction = "boost" | "epp";

// An inactive editor shows its saved target while retaining live capabilities.
// Hardware readback remains authoritative for the profile that is active now.
const cpuControlsForEditor = (controls: CpuPowerControls): CpuPowerControls => {
  const profile = controls.profile;
  if (!profile || profile.active) return controls;
  return {
    ...controls,
    cpu_boost: { ...controls.cpu_boost, enabled: profile.cpu_boost_enabled },
    epp: { ...controls.epp, value: profile.epp, numeric_value: parseNumericEpp(profile.epp) },
  };
};

interface CpuPowerControlsSectionProps {
  appId?: string;
  acProfile?: boolean;
  expectedAppId?: string;
  scopeLabel?: string;
  powerSource?: boolean;
  onBusyChange?: (owner: object, busy: boolean) => void;
}

const CpuPowerControlsSection: FC<CpuPowerControlsSectionProps> = ({
  appId = "", acProfile = false, expectedAppId = "", scopeLabel = "Global profile",
  powerSource = false, onBusyChange,
}: CpuPowerControlsSectionProps = {}) => {
  const [controls, setControls] = useState<CpuPowerControls | null>(null);
  const [loading, setLoading] = useState(false);
  const [changing, setChanging] = useState<CpuPowerAction | null>(null);
  const [eppPending, setEppPending] = useState(false);
  const [requestError, setRequestError] = useState("");
  const [eppDraft, setEppDraft] = useState(50);
  const visible = useQuickAccessVisible();

  const mountedRef = useRef(true);
  const visibleRef = useRef(visible);
  const controlsRef = useRef<CpuPowerControls | null>(null);
  const actionRef = useRef<CpuPowerAction | null>(null);
  const actionSequenceRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const readSequenceRef = useRef(0);
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
    if (!eppPendingRef.current) setEppDraft(eppSliderPercent(cpuControlsForEditor(next).epp));
  }, []);

  const rollbackEppDraft = useCallback(() => {
    const confirmed = controlsRef.current;
    if (confirmed) setEppDraft(eppSliderPercent(cpuControlsForEditor(confirmed).epp));
  }, []);

  const cancelEppDebounce = useCallback(() => {
    if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
    eppTimerRef.current = null;
    if (!eppPendingRef.current) return;
    eppPendingRef.current = false;
    setEppPending(false);
    if (!actionRef.current) reportBusy(false);
    rollbackEppDraft();
  }, [rollbackEppDraft, reportBusy]);

  const refreshControls = useCallback(async () => {
    if (!mountedRef.current || !visibleRef.current) return;
    // Visibility changes may request a refresh while a mutation is running.
    // Queue it; a getter must never supersede the authoritative setter reply.
    if (actionRef.current) {
      refreshAfterActionRef.current = true;
      return;
    }
    const readId = ++readSequenceRef.current;
    const mutationEpoch = mutationEpochRef.current;
    setLoading(true);
    try {
      const next = normaliseCpuPowerControls(await getCpuPowerControls(appId, acProfile));
      if (next.profile && (next.profile.app_id !== appId || next.profile.ac_profile !== acProfile)) {
        throw new Error("Backend returned CPU controls for a different profile.");
      }
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
      console.warn("[legotdp] CPU power controls RPC unavailable", error);
      const message = "CPU power controls are unavailable in this backend build.";
      acceptControls(unavailableCpuPowerControls(message));
      setRequestError(message);
    } finally {
      if (mountedRef.current && readId === readSequenceRef.current &&
          mutationEpoch === mutationEpochRef.current && !actionRef.current) {
        setLoading(false);
      }
    }
  }, [acceptControls, appId, acProfile]);

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
    let refreshNeeded = false;

    try {
      const next = normaliseCpuPowerControls(await request());
      if (next.profile && (next.profile.app_id !== appId || next.profile.ac_profile !== acProfile)) {
        throw new Error("Backend returned CPU controls for a different profile.");
      }
      if (!mountedRef.current) {
        if (!next.success) notify("CPU power change failed", next.error || next.epp.error || next.cpu_boost.error);
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
        notify(kind === "boost" ? "CPU Boost" : "EPP", message);
      }
    } catch (error) {
      if (!mountedRef.current) {
        notifyFailure("CPU power change failed", error);
        return;
      }
      if (actionId !== actionSequenceRef.current) {
        refreshNeeded = true;
        return;
      }
      refreshNeeded = true;
      if (kind === "epp") rollbackEppDraft();
      const message = error instanceof Error ? error.message : String(error);
      setRequestError(message);
      notifyFailure(kind === "boost" ? "CPU Boost change failed" : "EPP change failed", error);
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
    const boost = controlsRef.current && cpuControlsForEditor(controlsRef.current).cpu_boost;
    if (!visibleRef.current || !boost?.available || boost.enabled == null ||
        actionRef.current || eppPendingRef.current || enabled === boost.enabled) return;
    void runAction("boost", () => setCpuBoost(enabled, appId, acProfile, expectedAppId));
  };

  const changeEpp = (value: string) => {
    const epp = controlsRef.current && cpuControlsForEditor(controlsRef.current).epp;
    if (!visibleRef.current || !epp?.available || actionRef.current || value === epp.value) return;
    void runAction("epp", () => setEpp(value, appId, acProfile, expectedAppId));
  };

  const scheduleNumericEpp = (value: number) => {
    const epp = controlsRef.current && cpuControlsForEditor(controlsRef.current).epp;
    if (!mountedRef.current || !visibleRef.current || !epp?.available ||
        !epp.numeric_supported || actionRef.current) return;
    const percent = Math.round(clamp(value, 0, 100) / 10) * 10;
    const rawValue = Math.round(percent * 255 / 100);
    setEppDraft(percent);
    eppPendingRef.current = true;
    setEppPending(true);
    reportBusy(true);
    if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
    const flush = () => {
      if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
      eppTimerRef.current = null;
      flushEppRef.current = null;
      eppPendingRef.current = false;
      setEppPending(false);
      // Keep the scope and foreground game from this gesture, including when
      // a remount flushes the slider after switching game or battery/AC editor.
      void runAction("epp", () => setEpp(String(rawValue), appId, acProfile, expectedAppId));
    };
    flushEppRef.current = flush;
    eppTimerRef.current = setTimeout(flush, EPP_DEBOUNCE_MS);
  };

  if (!controls) return (
    <PanelSection title="CPU Power Controls">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  const editorControls = cpuControlsForEditor(controls);
  const boost = editorControls.cpu_boost;
  const epp = editorControls.epp;
  const busy = loading || changing !== null || eppPending;
  const numericCurrent = parseNumericEpp(epp.value);
  const eppLabel = epp.numeric_supported && numericCurrent != null
    ? "EPP"
    : `EPP - ${readableEppProfile(epp.value)}`;
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
  const driverManagedNumeric = epp.numeric_supported && numericCurrent == null &&
    (epp.value == null || EPP_NAMED_VALUES[epp.value] == null);
  const globalError = requestError || controls.error;

  return (
    <PanelSection title="CPU Power Controls">
      <PanelSectionRow>
        <Field label={scopeLabel} description={controls.profile?.active === false
          ? "Editing saved CPU Boost and EPP. They apply when this profile becomes active."
          : "CPU Boost and EPP changes are saved automatically to this profile."} />
      </PanelSectionRow>
      <PanelSectionRow>
        <ToggleField
          label="CPU Boost"
          description={
            !boost.available ? (boost.error || "Not supported by the active CPU frequency driver.")
            : boost.enabled == null ? (boost.error || "The current boost state could not be read.")
            : changing === "boost" ? "Applying through the kernel control and verifying its readback..."
            : boost.enabled ? "On - turbo frequencies are allowed."
            : "Off - turbo frequencies are disabled."
          }
          checked={boost.enabled === true}
          disabled={busy || !boost.available || boost.enabled == null}
          onChange={changeBoost}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        {epp.numeric_supported ? (
          <SliderField
            label={eppLabel}
            value={eppDraft}
            min={0}
            max={100}
            step={10}
            showValue={true}
            valueSuffix="%"
            disabled={loading || changing !== null || !epp.available}
            onChange={scheduleNumericEpp}
            description={
              !epp.available ? (epp.error || "EPP is not supported by the active CPU frequency driver.")
              : changing === "epp" ? "Applying and verifying on every CPU policy..."
              : eppPending ? "Waiting for the slider to settle before applying..."
              : driverManagedNumeric
                ? "Current profile is driver-managed. Move the slider to select an explicit value; 0% = Performance, 100% = Power saving."
              : "0% = Performance; 100% = Power saving. Applied in 10% steps."
            }
          />
        ) : (
          <SliderField
            label={eppLabel}
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
            description={
              !epp.available ? (epp.error || "EPP is not supported by the active CPU frequency driver.")
              : changing === "epp" ? "Applying and verifying on every CPU policy..."
              : noDiscreteChoice ? "The CPU driver exposes no alternative linear EPP profile."
              : profileIndex < 0
                ? `Choose a performance/efficiency profile: ${biasProfiles.map(readableEppProfile).join(" / ")}`
              : `Bias profiles: ${biasProfiles.map(readableEppProfile).join(" / ")}`
            }
          />
        )}
      </PanelSectionRow>
      {epp.compatibility_note && (
        <PanelSectionRow>
          <Field label="EPP compatibility" description={epp.compatibility_note} />
        </PanelSectionRow>
      )}
      {!epp.numeric_supported && specialProfiles.map((profile) => (
        <PanelSectionRow key={`epp-${profile}`}>
          <ButtonItem
            layout="below"
            disabled={busy || !epp.available || epp.value === profile}
            onClick={() => changeEpp(profile)}
          >
            {epp.value === profile
              ? `> ${readableEppProfile(profile)} EPP`
              : `Use ${readableEppProfile(profile)} EPP`}
          </ButtonItem>
        </PanelSectionRow>
      ))}
      {!controls.available && !globalError && (
        <PanelSectionRow>
          <div style={styles.infoBox}>CPU Boost and EPP are not supported on this device.</div>
        </PanelSectionRow>
      )}
      {globalError && (
        <PanelSectionRow><div style={styles.errorBox}>{globalError}</div></PanelSectionRow>
      )}
    </PanelSection>
  );
};

// ── Main content ───────────────────────────────────────────────────────────────
export const TdpPage: FC = () => {
  const [ready,    setReady]    = useState(false);
  const [setupErr, setSetupErr] = useState<string | null>(null);

  const [tuning,   setTuning]   = useState<Tuning>(fromAbsolute(15, 18, 25));
  const [acTuning, setAcTuning] = useState<Tuning>(fromAbsolute(15, 18, 25));
  const [preset,   setPreset]   = useState<PresetKey>("balanced");

  const [stdCaps, setStdCaps] = useState<Caps>(FALLBACK_STD);
  const [maxCaps, setMaxCaps] = useState<Caps>(FALLBACK_MAX);
  // Hardware with no ryzenadj path has nothing above the firmware to unlock.
  const [extrasAvailable, setExtrasAvailable] = useState(true);
  // The ladder is per machine, so it comes from the backend with the ceilings.
  const [presets, setPresets] = useState<PresetTable>(PRESETS);
  const [minW,    setMinW]    = useState(FALLBACK_MIN);

  const [enabled,       setEnabled]       = useState(true);
  const [game,          setGame]          = useState<RunningGame | null>(null);
  const [perGame,       setPerGame]       = useState(false);

  const [acOnline,      setAcOnline]      = useState(false);
  const [acSeparate,    setAcSeparate]    = useState(false);
  const [editingAc,     setEditingAc]     = useState(false);

  const [globalProfile, setGlobalProfile] = useState<{ spl: number; sppt: number; fppt: number; preset: string | undefined }>({ spl: 15, sppt: 18, fppt: 25, preset: undefined });
  const [extrasUnlocked, setExtrasUnlocked] = useState(false);

  const [savedPreset,   setSavedPreset]   = useState<string | undefined>(undefined);
  const [savedAcPreset, setSavedAcPreset] = useState<string | undefined>(undefined);

  const [status,   setStatus]   = useState<string | null>(null);
  const [loading,  setLoading]  = useState(false);
  const [cpuBusy, setCpuBusy] = useState(false);

  const visible = useQuickAccessVisible();

  const autoAppliedRef = useRef<string | null>(null);
  const noGameSyncedRef = useRef(false);
  const profileRequestRef = useRef(0);
  const statusTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cpuBusyOwnersRef = useRef(new Set<object>());
  const pageMountedRef = useRef(true);
  const onCpuBusyChange = useCallback((owner: object, busy: boolean) => {
    if (busy) cpuBusyOwnersRef.current.add(owner);
    else cpuBusyOwnersRef.current.delete(owner);
    if (pageMountedRef.current) setCpuBusy(cpuBusyOwnersRef.current.size > 0);
  }, []);

  useEffect(() => {
    pageMountedRef.current = true;
    return () => { pageMountedRef.current = false; };
  }, []);

  useEffect(() => () => { if (statusTimerRef.current) clearTimeout(statusTimerRef.current); }, []);

  const caps    = extrasUnlocked && extrasAvailable ? maxCaps : stdCaps;
  const active  = editingAc ? acTuning : tuning;
  const setActive = editingAc ? setAcTuning : setTuning;
  const handlers = makeTuningHandlers(active, setActive, caps, minW);
  const om      = offsetMax(active.spl, caps);

  const showStatus = (msg: string | null) => {
    if (statusTimerRef.current) clearTimeout(statusTimerRef.current);
    setStatus(msg);
    if (msg) statusTimerRef.current = setTimeout(() => setStatus(null), 3000);
  };

  /** Inline status plus a toast: the inline line clears after three seconds and
   *  lives in a section the user may not be looking at. */
  const showError = (title: string, e: unknown) => {
    notifyFailure(title, e);
    showStatus(`Error: ${e instanceof Error ? e.message : String(e)}`);
  };

  const applyGameProfile = async (
    gp: GameProfile,
    appId: string,
    statusMsg: string,
    isCurrent: () => boolean = () => AppWatcher.currentGame()?.appId === appId,
  ) => {
    if (!gp.exists || !gp.profile) {
      if (gp.exists) showStatus("Error: Game profile data is missing or corrupt.");
      return;
    }
    const p  = gp.profile;
    const t  = fromAbsolute(toW(p.spl), toW(p.sppt), toW(p.fppt));
    const ac = gp.ac_profile ?? { spl: p.spl, sppt: p.sppt, fppt: p.fppt, ac_preset: "" };
    const at = fromAbsolute(toW(ac.spl), toW(ac.sppt), toW(ac.fppt));
    const storedPreset = (p.preset as PresetKey | undefined) || undefined;
    try {
      const result = await applyTdp(p.spl, p.sppt, p.fppt, appId, "", appId);
      if (!result.success) throw new Error(result.stderr || "TDP apply failed");
    } catch (e: unknown) {
      if (isCurrent()) showError("Could not apply TDP", e);
      return false;
    }
    if (!isCurrent()) return false;
    setPerGame(true);
    setTuning(t);
    setAcTuning(at);
    setAcSeparate(gp.ac_separate);
    setEditingAc(false);
    setSavedPreset(storedPreset);
    setSavedAcPreset(gp.ac_separate ? (ac.ac_preset ?? "") : undefined);
    setPreset(storedPreset || detectPreset(toW(p.spl), toW(p.sppt), toW(p.fppt), presets));
    showStatus(statusMsg);
    return true;
  };

  // ── Init ─────────────────────────────────────────────────────────────────────
  useEffect(() => {
    let active = true;
    const check = async () => {
      try {
        const r = await isReady();
        if (!active) return;
        if (r.error) { setSetupErr(r.error); return; }
        if (r.ready) {
          const [s, ps, eu, c] = await Promise.all([
            getSettings(), getPowerSource(), getExtrasUnlocked(), getCaps(),
          ]);
          if (!active) return;
          const machinePresets = c?.presets ?? PRESETS;
          if (c?.std && c?.max) {
            setStdCaps(c.std); setMaxCaps(c.max); setMinW(c.min);
            setExtrasAvailable(c.extras !== false);
            setPresets(machinePresets);
          }
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          setTuning(fromAbsolute(w, sw, fw));
          setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
          setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw, machinePresets));
          setEnabled(s.enabled !== false);
          setAcOnline(ps.ac);
          setExtrasUnlocked(eu);
          setReady(true);
        } else {
          if (active) setTimeout(check, 1000);
        }
      } catch (_) { if (active) setTimeout(check, 1000); }
    };
    check();
    return () => { active = false; };
  }, []);

  // ── Game detection ────────────────────────────────────────────────────────────
  // AppWatcher owns this and runs for the whole session, so the backend keeps
  // getting the authoritative appid while the panel is closed. Here we only
  // adopt what it reports.
  useEffect(() => {
    setGame(AppWatcher.currentGame());
    return AppWatcher.listen(setGame);
  }, []);

  // ── AC state ──────────────────────────────────────────────────────────────────
  // The enforce loop already reads the charger every five seconds to decide
  // which profile applies, and now emits when the answer changes - so the panel
  // subscribes instead of running its own three-second poll. The one read on
  // open seeds the label, since an event only fires on a change and the last one
  // may have happened while the panel was shut.
  useEffect(() => {
    if (!ready || !visible) return;
    let active = true;
    const onPower = (ps: PowerSource) => { if (active) setAcOnline(ps.ac); };
    addEventListener<[PowerSource]>("power_source", onPower);
    getPowerSource().then((ps) => { if (active) setAcOnline(ps.ac); }).catch(() => undefined);
    return () => {
      active = false;
      removeEventListener<[PowerSource]>("power_source", onPower);
    };
  }, [ready, visible]);

  // ── Auto-apply game profile when game / ready / enabled changes ──────────────
  useEffect(() => {
    if (!ready) return;
    const request = ++profileRequestRef.current;
    const current = (expected: string) =>
      request === profileRequestRef.current &&
      (AppWatcher.currentGame()?.appId ?? "") === expected;

    if (!enabled) {
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      noGameSyncedRef.current = false;
      return;
    }

    if (!game) {
      const wasInGame = autoAppliedRef.current !== null;
      if (perGame) setPerGame(false);
      autoAppliedRef.current = null;
      // setPerGame above re-runs this effect (perGame is a dependency), and
      // without this the whole no-game branch ran twice per game exit - a
      // second getSettings for a state we had already adopted.
      if (noGameSyncedRef.current) return;
      noGameSyncedRef.current = true;
      setSavedPreset(undefined);
      setSavedAcPreset(undefined);
      setAcSeparate(false);
      setEditingAc(false);
      (async () => {
        try {
          const s = await getSettings();
          if (!current("")) return;
          const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
          setTuning(fromAbsolute(w, sw, fw));
          setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw, presets));
          setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
          if (wasInGame) {
            const result = await applyTdp(
              s.spl, s.sppt, s.fppt, "", s.active_preset || "", "");
            if (!result.success) throw new Error(result.stderr || "TDP apply failed");
            if (!current("")) return;
            showStatus("Global settings restored.");
          }
        } catch (e: unknown) {
          showError("LeGoTDP", e);
        }
      })();
      return () => { profileRequestRef.current += 1; };
    }

    noGameSyncedRef.current = false;
    if (autoAppliedRef.current === game.appId) return;
    autoAppliedRef.current = game.appId;

    (async () => {
      try {
        const requestedGame = game;
        const gp = await getGameProfile(requestedGame.appId);
        if (!current(requestedGame.appId)) return;
        await applyGameProfile(
          gp,
          requestedGame.appId,
          `Auto-applied profile for ${requestedGame.name}.`,
          () => current(requestedGame.appId),
        );
      } catch (e: unknown) {
        if (current(game.appId)) {
          autoAppliedRef.current = null;
          showError("LeGoTDP", e);
        }
      }
    })();
    return () => { profileRequestRef.current += 1; };
  }, [game?.appId, ready, enabled]);

  // ── Preset handler ────────────────────────────────────────────────────────────
  const handlePresetChange = async (key: PresetKey) => {
    const prevPreset = preset;
    const prevTuning = tuning, prevAcTuning = acTuning;
    setPreset(key);
    if (key === "custom") return;

    const vals = presets[key];
    const next = normalise(fromAbsolute(vals.spl, vals.sppt, vals.fppt), caps, minW);
    if (editingAc) setAcTuning(next); else setTuning(next);

    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    const a = absolute(next);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, key);
        if (r.success) {
          setSavedAcPreset(key);
        } else {
          setPreset(prevPreset);
          setAcTuning(prevAcTuning);
        }
        showStatus(r.success ? `AC: ${PRESET_LABELS[key]} saved for ${game!.name}.` : `Error: ${r.stderr || "unknown"}`);
      } else {
        const r = await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, key,
          AppWatcher.currentGame()?.appId ?? "");
        if (r.success) {
          if (!appId) setGlobalProfile({ ...a, preset: key });
          else setSavedPreset(key);
        } else {
          setPreset(prevPreset);
          setTuning(prevTuning);
        }
        showStatus(r.success
          ? (appId ? `${PRESET_LABELS[key]} saved for ${game!.name}.` : `${PRESET_LABELS[key]} applied.`)
          : `Error: ${r.stderr || "unknown"}`
        );
      }
    } catch (e: unknown) {
      setPreset(prevPreset);
      if (editingAc) setAcTuning(prevAcTuning); else setTuning(prevTuning);
      showError("LeGoTDP", e);
    }
    setLoading(false);
  };

  // ── Per-game toggle ───────────────────────────────────────────────────────────
  const handlePerGameToggle = async (checked: boolean) => {
    if (cpuBusyOwnersRef.current.size > 0) return;
    setPerGame(checked);
    if (!checked && game) {
      const prevAcSeparate = acSeparate, prevEditingAc = editingAc;
      const prevSavedPreset = savedPreset, prevSavedAcPreset = savedAcPreset;
      setAcSeparate(false);
      setEditingAc(false);
      setSavedPreset(undefined);
      setSavedAcPreset(undefined);
      let profileDeleted = false;
      try {
        const deleted = await deleteGameProfile(game.appId);
        if (!deleted.success) throw new Error(deleted.stderr || "Could not delete profile");
        profileDeleted = true;
        const s = await getSettings();
        const w = toW(s.spl), sw = toW(s.sppt), fw = toW(s.fppt);
        setTuning(fromAbsolute(w, sw, fw));
        setPreset((s.active_preset as PresetKey | undefined) || detectPreset(w, sw, fw, presets));
        setGlobalProfile({ spl: w, sppt: sw, fppt: fw, preset: s.active_preset || undefined });
        showStatus("Switched to global settings.");
      } catch (e: unknown) {
        if (!profileDeleted) {
          setPerGame(true);
          setAcSeparate(prevAcSeparate); setEditingAc(prevEditingAc);
          setSavedPreset(prevSavedPreset); setSavedAcPreset(prevSavedAcPreset);
        }
        showError("LeGoTDP", e);
      }
      // Cleared last so the auto-apply effect cannot race the delete above.
      autoAppliedRef.current = profileDeleted ? game.appId : null;
    } else if (checked && game) {
      try {
        const gp = await getGameProfile(game.appId);
        if (!gp.exists) {
          setSavedPreset(undefined);
          setSavedAcPreset(undefined);
          showStatus(`No saved profile for ${game.name}. Use sliders to create one.`);
          autoAppliedRef.current = game.appId;
        } else {
          await applyGameProfile(gp, game.appId, `Profile applied for ${game.name}.`);
        }
      } catch (e: unknown) {
        setPerGame(false);
        showError("LeGoTDP", e);
      }
    }
  };

  // ── Enable / disable plugin ───────────────────────────────────────────────────
  const handleEnabledToggle = async (checked: boolean) => {
    if (cpuBusyOwnersRef.current.size > 0) return;
    setEnabled(checked);
    showStatus(null);
    try {
      const result = await setPluginEnabled(checked);
      if (!result.success) throw new Error(result.stderr || "Could not change plugin state");
      showStatus(checked ? "Plugin enabled." : "Plugin disabled. Firmware defaults restored.");
    } catch (e: unknown) {
      setEnabled(!checked);
      showError("LeGoTDP", e);
    }
  };

  // ── AC separate toggle ────────────────────────────────────────────────────────
  const handleAcSeparateToggle = async (checked: boolean) => {
    if (cpuBusyOwnersRef.current.size > 0) return;
    if (!game) return;
    const prevSavedAcPreset = savedAcPreset;
    const prevEditingAc = editingAc;
    const prevAcTuning = acTuning;
    setAcSeparate(checked);
    let use = acTuning;
    if (checked && savedAcPreset === undefined) {
      use = tuning;
      setAcTuning(tuning);
    }
    if (!checked) {
      setEditingAc(false);
      setSavedAcPreset(undefined);
    }
    const a = absolute(use);
    try {
      const result = await setGameAcProfile(
        game.appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), checked, "");
      if (!result.success) throw new Error(result.stderr || "Could not save AC profile");
    } catch (e: unknown) {
      setAcSeparate(!checked);
      setSavedAcPreset(prevSavedAcPreset);
      setEditingAc(prevEditingAc);
      setAcTuning(prevAcTuning);
      showError("LeGoTDP", e);
    }
  };

  // ── Extras: unlock extended TDP range ────────────────────────────────────────
  const handleExtrasUnlockedToggle = async (checked: boolean) => {
    setExtrasUnlocked(checked);
    try {
      const result = await setExtrasUnlockedCall(checked);
      if (!result.success) throw new Error(result.stderr || "Could not change Extras range");
    } catch (e: unknown) {
      setExtrasUnlocked(!checked);
      showError("LeGoTDP", e);
      return;
    }
    if (checked) return;

    // The backend clamps every persisted target and the active hardware change
    // in one transaction. Mirror that result locally without issuing a second
    // apply that could race a game or charger transition.
    const t  = normalise(tuning,   stdCaps, minW);
    const at = normalise(acTuning, stdCaps, minW);
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
  };

  // ── Apply (Custom mode only) ──────────────────────────────────────────────────
  const apply = async () => {
    setLoading(true);
    showStatus(null);
    const appId = (perGame && game) ? game.appId : "";
    const a = absolute(active);
    try {
      if (editingAc && appId) {
        const r = await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, "custom");
        if (r.success) setSavedAcPreset("custom");
        showStatus(r.success ? `AC profile saved for ${game!.name}.` : `Error: ${r.stderr || "unknown"}`);
      } else {
        const r = await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, "custom",
          AppWatcher.currentGame()?.appId ?? "");
        if (r.success) {
          if (!appId) setGlobalProfile({ ...a, preset: "custom" });
          else setSavedPreset("custom");
        }
        showStatus(r.success
          ? (appId ? `Profile saved for ${game!.name}.` : "Custom settings applied.")
          : `Error: ${r.stderr || "unknown"}`
        );
      }
    } catch (e: unknown) {
      showError("LeGoTDP", e);
    }
    setLoading(false);
  };

  // ── Render ────────────────────────────────────────────────────────────────────
  if (setupErr) return (
    <PanelSection title="Setup Error">
      <PanelSectionRow><Field label="Error" description={setupErr} /></PanelSectionRow>
    </PanelSection>
  );

  if (!ready) return (
    <PanelSection title="Initializing...">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  return (
    <>
      <PanelSection title="LeGoTDP">
        <PanelSectionRow>
          <ToggleField
            label="Enable"
            description={
              enabled ? (
                <span>
                  <span style={{ fontSize: "11px", color: DIM_COLOR }}>Global Profile: </span>
                  <span style={styles.profileTag}>{profileLabel(globalProfile.spl, globalProfile.sppt, globalProfile.fppt, globalProfile.preset, presets)}</span>
                  {!extrasUnlocked && exceedsCaps(globalProfile.spl, globalProfile.sppt, globalProfile.fppt, stdCaps) && (
                    <span style={{ fontSize: "11px", color: WARN_COLOR }}> ⚠ exceeds firmware limits</span>
                  )}
                </span>
              ) : "Using system defaults"
            }
            checked={enabled}
            disabled={cpuBusy}
            onChange={handleEnabledToggle}
          />
        </PanelSectionRow>
        {status && !enabled && (
          <PanelSectionRow>
            <div style={statusStyle(status)}>
              {status}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      <LivePanel />
      <CpuPowerControlsSection
        key={`${enabled}:${game?.appId ?? ""}:${perGame}:${acSeparate && editingAc}`}
        appId={enabled && perGame && game ? game.appId : ""}
        acProfile={!!(enabled && perGame && game && acSeparate && editingAc)}
        expectedAppId={game?.appId ?? ""}
        scopeLabel={enabled && perGame && game
          ? `${game.name}${acSeparate ? ` - ${editingAc ? "AC" : "Battery"}` : ""} profile`
          : "Global profile"}
        powerSource={acOnline}
        onBusyChange={onCpuBusyChange}
      />

      {enabled && <>
        <PanelSection title="Game Profile">
          <PanelSectionRow>
            <ToggleField
              label="Per Game Profile"
              description={
                game ? (
                  perGame ? (
                    <span style={{ display: "flex", flexDirection: "column", gap: "3px" }}>
                      <span>{game.name}</span>
                      <span style={{ display: "flex", flexDirection: "column", gap: "1px" }}>
                        <span>
                          <span style={{ fontSize: "11px", color: DIM_COLOR }}>Battery: </span>
                          <span style={styles.profileTag}>
                            {profileLabel(absolute(tuning).spl, absolute(tuning).sppt, absolute(tuning).fppt, savedPreset, presets)}
                          </span>
                        </span>
                        {acSeparate && (
                          <span>
                            <span style={{ fontSize: "11px", color: DIM_COLOR }}>AC: </span>
                            <span style={styles.profileTag}>
                              {profileLabel(absolute(acTuning).spl, absolute(acTuning).sppt, absolute(acTuning).fppt, savedAcPreset, presets)}
                            </span>
                          </span>
                        )}
                      </span>
                    </span>
                  ) : game.name
                ) : "No game running"
              }
              checked={perGame}
              disabled={!game || cpuBusy}
              onChange={handlePerGameToggle}
            />
          </PanelSectionRow>
          {perGame && (
            <PanelSectionRow>
              <ToggleField
                label="Separate AC Profile"
                description={acSeparate
                  ? "AC and battery have independent TDP, CPU Boost and EPP settings"
                  : "Enable separate TDP, CPU Boost and EPP settings when charging"}
                checked={acSeparate}
                disabled={cpuBusy}
                onChange={handleAcSeparateToggle}
              />
            </PanelSectionRow>
          )}
          {perGame && acSeparate && (
            <>
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={() => {
                  if (!cpuBusyOwnersRef.current.size) setEditingAc(false);
                }} disabled={!editingAc || cpuBusy}>
                  {!editingAc ? "> Battery profile" : "Battery profile"}
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={() => {
                  if (!cpuBusyOwnersRef.current.size) setEditingAc(true);
                }} disabled={editingAc || cpuBusy}>
                  {editingAc ? "> AC profile" : "AC profile"}
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <div style={{ fontSize: "11px", fontWeight: "bold", color: acOnline ? OK_COLOR : WARN_COLOR }}>
                  {acOnline ? "Charging (AC)" : "On battery"}
                </div>
              </PanelSectionRow>
            </>
          )}
        </PanelSection>

        <PanelSection title="Preset">
          {PRESET_ORDER.map(key => (
            <PanelSectionRow key={key}>
              <ButtonItem
                layout="below"
                disabled={preset === key || loading}
                onClick={() => handlePresetChange(key)}
              >
                {preset === key ? `> ${PRESET_LABELS[key]}` : PRESET_LABELS[key]}
              </ButtonItem>
            </PanelSectionRow>
          ))}
          {status && preset !== "custom" && (
            <PanelSectionRow>
              <div style={statusStyle(status)}>
                {status}
              </div>
            </PanelSectionRow>
          )}
        </PanelSection>

        {preset === "custom" && (
          <>
            <PanelSection title={editingAc ? "TDP Limits (AC)" : "TDP Limits"}>
              <PanelSectionRow>
                <SliderField
                  label={`SPL (TDP) - ${active.spl} W`}
                  value={active.spl} min={minW} max={caps.spl} step={1}
                  onChange={handlers.onSpl}
                  description="Sustained power limit - the main TDP dial"
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`SPPT +${active.spptOff} W  =  ${active.spl + active.spptOff} W`}
                  value={active.spptOff} min={0} max={om.sppt || 1} step={1}
                  disabled={om.sppt === 0}
                  onChange={handlers.onSppt}
                  description={om.sppt === 0
                    ? "No headroom left at this SPL"
                    : `Slow limit headroom above SPL (max +${om.sppt} W here)`}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  label={`FPPT +${active.fpptOff} W  =  ${active.spl + active.fpptOff} W`}
                  value={active.fpptOff} min={0} max={om.fppt || 1} step={1}
                  disabled={om.fppt === 0}
                  onChange={handlers.onFppt}
                  description={om.fppt === 0
                    ? "No headroom left at this SPL"
                    : `Fast limit headroom above SPL (max +${om.fppt} W here)`}
                />
              </PanelSectionRow>
            </PanelSection>

            <PanelSection title="Action">
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={apply} disabled={loading}>
                  {loading ? "Applying..."
                    : editingAc && game ? `Save AC for ${game.name}`
                    : perGame && game ? `Apply & Save for ${game.name}`
                    : "Apply TDP"}
                </ButtonItem>
              </PanelSectionRow>
              {status && (
                <PanelSectionRow>
                  <div style={statusStyle(status)}>
                    {status}
                  </div>
                </PanelSectionRow>
              )}
            </PanelSection>
          </>
        )}
      </>}


      {!extrasAvailable && extrasUnlocked && (
        <PanelSection title="Extras temporarily unavailable">
          <PanelSectionRow>
            <Field label="Saved profiles are preserved"
              description="Firmware limits are active until the verified Extras helper is available again." />
          </PanelSectionRow>
          <PanelSectionRow>
            <ButtonItem disabled={loading} onClick={async () => {
              setLoading(true);
              try {
                const result = await retryExtras();
                if (!result.success) throw new Error(result.error || "Retry failed");
                const c = await getCaps();
                setStdCaps(c.std); setMaxCaps(c.max); setMinW(c.min);
                setExtrasAvailable(c.extras !== false);
              } catch (error) { notifyFailure("Extras recovery failed", error); }
              finally { setLoading(false); }
            }}>Retry Extras download</ButtonItem>
          </PanelSectionRow>
        </PanelSection>
      )}
      {extrasAvailable && (
        <PanelSection title="Extras">
          <PanelSectionRow>
            <div style={styles.infoBox}>
              These settings are for advanced users only and are NOT recommended.
              Changes are made at your own risk — they override the manufacturer's TDP safety limits.
            </div>
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label={`Unlock Custom TDP to ${maxCaps.spl} W`}
              description={extrasUnlocked
                ? `Custom sliders extended to ${maxCaps.spl} W - applied via ryzenadj instead of firmware`
                : `Enable to allow Custom sliders up to ${maxCaps.spl} W`}
              checked={extrasUnlocked}
              onChange={handleExtrasUnlockedToggle}
            />
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
};

// ── Plugin entry point ─────────────────────────────────────────────────────────

export const startTdpWatcher = () => AppWatcher.start();
export const stopTdpWatcher = () => AppWatcher.stop();
