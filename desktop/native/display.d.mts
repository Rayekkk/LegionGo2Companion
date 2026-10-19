// SPDX-License-Identifier: BSD-3-Clause
// Types of display.mjs for the Decky panel (the Desktop app imports the module from QML).
type State = { settings_error?: string; reset_in_progress?: boolean; reset_error?: string; setup_done?: boolean; panel_ok?: boolean;
  restart_pending?: boolean; panel_mode?: string | null; reset_restart_pending?: boolean };
export const RESET_RETRY: string;
export function displayPaused(state?: State | null): [string, string] | null;
export function brightnessShown(state?: State | null): boolean;
export function firstRunLocked(state?: State | null): boolean;
