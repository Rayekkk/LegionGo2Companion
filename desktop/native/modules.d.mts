// SPDX-License-Identifier: BSD-3-Clause
// Types of modules.mjs for the Decky panel (the Desktop app imports the module from QML).
type Name = "tdp" | "fan" | "vibration" | "rgb" | "remap" | "controller" | "battery" | "display" | "wifi";
type Entry = { applicable?: boolean; enabled?: boolean; ready?: boolean; pending?: boolean; maintenance?: boolean; starting?: boolean; error?: string; note?: string };
export const MODULE_LABELS: Record<Name, string>;
export const MODULE_DESCRIPTIONS: Record<Name, string>;
export function moduleStartingText(entry?: Entry | null): string;
export function moduleChecked(entry?: Entry | null): boolean;
export function moduleLocked(entry?: Entry | null): boolean;
type Device = { console?: string | null; model?: string | null; firmware?: boolean; power_led?: boolean };
export function moduleDescription(name: string, device?: Device | null): string;
export function moduleNote(name: string, entry?: Entry | null, device?: Device | null): string;
export function moduleShown(entry?: Entry | null): boolean;
export function cleanupRetryShown(entry?: Entry | null): boolean;
export function startRetryShown(entry?: Entry | null): boolean;
