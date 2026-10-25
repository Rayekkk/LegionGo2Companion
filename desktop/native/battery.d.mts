// SPDX-License-Identifier: BSD-3-Clause
// Types of battery.mjs for the Decky panel (the Desktop app imports the module from QML).
type Protection = { supported?: boolean; managed?: boolean; enabled?: boolean | null; requested_enabled?: boolean | null; recovery_pending?: boolean; mode_conflict?: boolean };
type Levels = { left?: number | null; right?: number | null; connection_left?: string | null; connection_right?: string | null };
export function levelText(value: unknown): string;
export function controllersText(levels?: Levels | null): string;
export function protectionChecked(status?: Protection | null): boolean;
export function protectionLocked(status?: Protection | null): boolean;
export function protectionNote(status?: Protection | null): string;
export function resetShown(status?: Protection | null): boolean;
export const RESET_LABEL: string;
export const RESET_NOTE: string;
export const RESET_DONE: string;
