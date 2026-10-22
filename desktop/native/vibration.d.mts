// SPDX-License-Identifier: BSD-3-Clause
// Types of vibration.mjs for the Decky panel (the Desktop app imports the module from QML).
export const LEVELS: string[];
export const LEVELS_SHORT: string[];
export const FALLBACK_MODES: string[];
export function modeLabel(raw: string): string;
export function modeShort(raw: string): string;
export function driverNotice(status: { found?: boolean } | null | undefined): [string, string] | null;
export function strengthOnly(device?: { console?: string | null } | null): boolean;
export const BETA_TEXT: string;
export function touchpadStrengthEditable(settings?: { touchpadEnabled?: boolean } | null): boolean;
export function failureText(what: string, error?: string): string;
