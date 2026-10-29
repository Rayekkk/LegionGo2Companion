// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP
// TDP page: backend calls, their types, unit helpers and toasts.

import { toaster } from "@decky/api";
import { errorText, rpc } from "./shared";
import {
  fromAbsolute, resolvedPreset, simpleTuning, type Caps, type CapsInfo, type CpuPowerControls, type PresetTable,
  type RunningLimits, type TdpInfo,
} from "../desktop/native/tdp.mjs";


// ── Helpers ────────────────────────────────────────────────────────────────────
export const toMw  = (w: number)  => w * 1000;
export const toW   = (mw: number) => Math.round(mw / 1000);

// The slider model, presets and texts are in desktop/native/tdp.mjs, shared with the Desktop app.

/** Slider view of saved limits (mW). In simple mode a Custom profile follows the single-dial rule. */
export function savedTuning(limits: { spl: number; sppt: number; fppt: number }, stored: string | undefined,
                     table: PresetTable, advanced: boolean, caps: Caps, minW: number) {
  const spl = toW(limits.spl), sppt = toW(limits.sppt), fppt = toW(limits.fppt);
  const preset = resolvedPreset(spl, sppt, fppt, stored, table);
  return { preset, tuning: !advanced && preset === "custom" ? simpleTuning(spl, caps, minW) : fromAbsolute(spl, sppt, fppt) };
}

// ── Types ──────────────────────────────────────────────────────────────────────
export interface Settings   { spl: number; sppt: number; fppt: number; enabled: boolean;
                       active_preset?: string; advanced_tdp_control?: boolean; separate_battery_presets?: boolean;
                       running?: RunningLimits }
export interface TdpResult  { success: boolean; stderr?: string; skipped?: boolean; returncode?: number }
export interface PowerSource { ac: boolean }
export interface GameProfile {
  error?: string;
  exists: boolean;
  // False: the profile is kept but unused; the game runs on Global settings.
  active?: boolean;
  profile: { spl: number; sppt: number; fppt: number; preset?: string };
  ac_separate: boolean;
  ac_profile: { spl: number; sppt: number; fppt: number; ac_preset?: string };
}
export interface RunningGame { appId: string; name: string }
export interface ReadyState  { ready: boolean; error: string }

// ── Backend callables ──────────────────────────────────────────────────────────
export const isReady           = rpc<[], ReadyState>("is_ready");
export const getSettings       = rpc<[], Settings>("get_settings");
export const getCaps           = rpc<[], CapsInfo>("get_caps");
export const applyTdp          = rpc<[number, number, number, string, string, string | null], TdpResult>("apply_tdp");
export const getTdpInfo        = rpc<[], TdpInfo>("get_tdp_info");
export const getGameProfile    = rpc<[string], GameProfile>("get_game_profile");
export const deleteGameProfile = rpc<[string], TdpResult>("delete_game_profile");
export const setGameProfileActive = rpc<[string, boolean], TdpResult>("set_game_profile_active");
export const setPluginEnabled  = rpc<[boolean], TdpResult>("set_plugin_enabled");
export const setPanelActive    = rpc<[boolean], void>("set_panel_active");
export const setActiveApp      = rpc<[string], void>("set_active_app");
export const getPowerSource    = rpc<[], PowerSource>("get_power_source");
export const reapply           = rpc<[], TdpResult>("reapply");
export const setGameAcProfile  = rpc<[string, number, number, number, boolean, string], TdpResult>("set_game_ac_profile");
export const retryExtras = rpc<[], { success: boolean; error?: string }>("retry_extras");
export const getExtrasUnlocked = rpc<[], boolean>("get_extras_unlocked");
export const setExtrasUnlockedCall = rpc<[boolean], TdpResult>("set_extras_unlocked");
export const setAdvancedTdpControlCall = rpc<[boolean], TdpResult>("set_advanced_tdp_control");
export const setSeparateBatteryPresetsCall = rpc<[boolean], TdpResult>("set_separate_battery_presets");
export const getCpuPowerControls = rpc<[string, boolean], CpuPowerControls>("get_cpu_power_controls");
export const setCpuBoost       = rpc<[boolean, string, boolean, string], CpuPowerControls>("set_cpu_boost");
export const setEpp            = rpc<[string, string, boolean, string], CpuPowerControls>("set_epp");

// ── Toasts ─────────────────────────────────────────────────────────────────────

export const notify = (title: string, body: string) => {
  try {
    toaster.toast({ title, body, duration: 4000 });
  } catch {
    console.error("[legiongo2companion] tdp.notification_failed");
  }
};

export const notifyFailure = (title: string, err: unknown) => {
  const body = errorText(err ?? "Unknown error");
  console.error("[legiongo2companion] tdp.operation_failed");
  notify(title, body);
};

