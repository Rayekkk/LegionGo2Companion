// SPDX-License-Identifier: BSD-3-Clause
// Display rules and texts, shared by the Desktop app and the Decky panel (src/display.tsx imports this file).

export const RESET_RETRY = "Retry display reset";

/** Why every display control is paused, as [title, detail], or null. Only the reset stays available. */
export function displayPaused(state) {
  if (state?.settings_error) return ["Display controls are paused", state.settings_error];
  if (state?.reset_in_progress) return ["Display controls are paused",
    state.reset_error || "An earlier reset did not finish. Retry to restore the display and remove the Companion script."];
  return null;
}

/** The HDR brightness slider needs the installed fix, a panel it can drive and no pending restart. */
export const brightnessShown = state => !!state?.setup_done && !!state.panel_ok && !state.restart_pending;

/** First-run mode choices wait for the Gaming Mode restart that finishes a reset. */
export const firstRunLocked = state => !state?.panel_mode && !!state?.reset_restart_pending;
