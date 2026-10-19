// SPDX-License-Identifier: BSD-3-Clause
// Types of vibration.mjs for the Decky panel (the Desktop app imports the module from QML).
export const LEVELS: string[];
export const LEVELS_SHORT: string[];
export const FALLBACK_MODES: string[];
export function modeLabel(raw: string): string;
export function modeShort(raw: string): string;
export function touchpadStrengthEditable(settings?: { touchpadEnabled?: boolean } | null): boolean;
