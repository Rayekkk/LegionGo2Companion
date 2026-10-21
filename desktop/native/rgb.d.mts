// SPDX-License-Identifier: BSD-3-Clause
// Types of rgb.mjs for the Decky panel (the Desktop app imports the module from QML).
type Settings = { control_enabled?: boolean; rings_enabled?: boolean; effect?: string; power_led_managed?: boolean; power_led_enabled?: boolean };
type Status = {
  settings?: Settings;
  rgb?: { supported?: boolean; reason?: string; drift?: boolean };
  power_led?: { supported?: boolean; reason?: string; current?: boolean | null; drift?: boolean; beta?: boolean };
};
export const RESTORE_NOTE: string;
export const POWER_LED_BETA_TEXT: string;
export const SPEEDS: { id: number; label: string }[];
export function effectsEditable(settings?: Settings | null): boolean;
export function usesColor(settings?: Settings | null): boolean;
export function usesSpeed(settings?: Settings | null): boolean;
export function speedStep(speed: number): number;
export function controlNote(status?: Status | null): string;
export function ringsNote(status?: Status | null): string;
export function powerChecked(status?: Status | null): boolean;
export function powerNote(status?: Status | null): string;
