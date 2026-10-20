// SPDX-License-Identifier: BSD-3-Clause
// Types of tdp.mjs for the Decky panel (the Desktop app imports the module from QML).
export interface Tuning { spl: number; spptOff: number; fpptOff: number }
export interface Caps { spl: number; sppt: number; fppt: number }
export type PresetKey = "silent" | "balanced" | "performance" | "max" | "custom";
export type PresetTable = Record<Exclude<PresetKey, "custom">, Caps>;
export interface CapsInfo {
  min: number; std: Caps; max: Caps; wmi?: boolean; extras?: boolean; presets?: PresetTable; battery_presets?: PresetTable;
  tdp_available?: boolean; tdp_error?: string; helper_retry_available?: boolean;
}
export interface CapsState { tdpAvailable: boolean; extrasAvailable: boolean; helperRetryAvailable: boolean; tdpError: string }
export interface TdpValues { spl_limit?: number; sppt_limit?: number; fppt_limit?: number; package_draw?: number; source?: string }
export interface TdpInfo { success: boolean; values: TdpValues; error?: string }
export interface SettingsLimits {
  spl?: number; sppt?: number; fppt?: number; active_spl?: number; active_sppt?: number; active_fppt?: number; active_preset?: string;
  battery_preset?: string;
}
export interface CpuBoostControl { available: boolean; enabled: boolean | null; error: string }
export interface EppControl {
  compatibility_note?: string; available: boolean; value: string | null; profiles: string[];
  numeric_supported: boolean; numeric_value: number | null; min: number; max: number; error: string;
}
/** Shared payload for the CPU power controls getter and both setters. */
export interface CpuPowerControls {
  success: boolean; available: boolean; cpu_boost: CpuBoostControl; epp: EppControl; error: string;
  profile?: { app_id: string; ac_profile: boolean; active: boolean; cpu_boost_enabled: boolean | null; epp: string | null };
}

export const OFFSET_MAX: { sppt: number; fppt: number };
export const FALLBACK_STD: Caps;
export const FALLBACK_MAX: Caps;
export const FALLBACK_MIN: number;
export function offsetMax(spl: number, caps: Caps): { sppt: number; fppt: number };
export function normalise(t: Tuning, caps: Caps, minW: number): Tuning;
export function absolute(t: Tuning): Caps;
export function fromAbsolute(spl: number, sppt: number, fppt: number): Tuning;
export function simpleCaps(caps: Caps, unlocked: boolean): Caps;
export function simpleTuning(spl: number, caps: Caps, minW: number): Tuning;
export function withSpl(t: Tuning, v: number, caps: Caps, minW: number): Tuning;
export function withSppt(t: Tuning, v: number, caps: Caps): Tuning;
export function withFppt(t: Tuning, v: number, caps: Caps): Tuning;

export const PRESETS: PresetTable;
export const PRESET_LABELS: Record<PresetKey, string>;
export const PRESET_ORDER: PresetKey[];
export function detectPreset(spl: number, sppt: number, fppt: number, table?: PresetTable): PresetKey;
export function resolvedPreset(spl: number, sppt: number, fppt: number, stored: string | undefined, table: PresetTable): PresetKey;
export function exceedsCaps(spl: number, sppt: number, fppt: number, caps: Caps): boolean;
export function activeLimits(settings: SettingsLimits | null | undefined, table?: PresetTable):
  { spl: number; sppt: number; fppt: number; global: boolean; label: string } | null;
export function overviewSummary(settings: (SettingsLimits & { enabled?: boolean }) | null | undefined, table?: PresetTable): string;
export function presetNote(key: PresetKey, table: PresetTable): string;
export const BATTERY_PRESETS_LABEL: string;
export const BATTERY_PRESETS_NOTE: string;
export function shownPresets(table: PresetTable, batteryTable: PresetTable | null | undefined, separate: boolean,
  onBattery: boolean): PresetTable;

export function powerSummary(enabled: boolean, owner: string, label: string, now: Caps, draw: number | null | undefined,
  unlocked: boolean, std: Caps): string;
export function scopeNote(use: "global" | "game" | "charger", name: string): string;
export function sourceNote(acOnline: boolean, charger: boolean): string;
export function boostRoomNote(spl: number, offset: number, room: number): string;
export function capsState(caps: Partial<CapsInfo> | null | undefined): CapsState;
export function tdpNotice(state: CapsState, unlocked: boolean): [string, string] | null;
export function extrasShown(state: CapsState): boolean;
export function helperRetryShown(state: CapsState, unlocked: boolean): boolean;
export function extrasNote(unlocked: boolean, state: CapsState, std: Caps): string;
export function extrasConfirm(state: CapsState): { title: string; ok: string; text: string };
export function limitsText(info: TdpInfo | null | undefined): string;
export function sourceText(info: TdpInfo | null | undefined): string;

export const EPP_NAMED_VALUES: Record<string, number>;
export const EPP_PREFERENCE: string;
export function eppLabel(profile: string | null | undefined): string;
export function parseNumericEpp(value: string | null | undefined): number | null;
export function editorControls(controls: CpuPowerControls): CpuPowerControls;
export function eppPercent(epp: EppControl, step: number): number;
export function eppValue(percent: number): string;
export function eppTitle(epp: EppControl): string;
export function eppUnavailable(epp: EppControl): string;
export function eppNote(epp: EppControl): string;
export function boostUsable(boost: CpuBoostControl): boolean;
export function boostNote(boost: CpuBoostControl): string;
