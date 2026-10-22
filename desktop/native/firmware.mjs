// SPDX-License-Identifier: BSD-3-Clause
// Controller firmware texts, shared by the Desktop app and the Decky panel (src/firmware.tsx imports this file).
// The steps and their gates stay in each UI; this file only names them.

export const PHASE_LABELS = { idle: "Controller firmware", checking: "Checking for updates", up_to_date: "Up to date",
  available: "Update available", downloading: "Downloading update", downloaded: "Update downloaded", installing: "Installing update",
  verifying: "Verifying update", installed: "Update installed", failed: "Update failed" };
/** Shown above the updater in both UIs; delete this constant and its two uses once the next real controller update has been verified. */
export const RISK_NOTICE = { title: "Risk notice",
  text: "This is the only Companion feature that could actually damage your hardware. The updater was tested on the developer's own Legion Go 2 and passed every check, but only by reinstalling the firmware already installed: an update to a newer version has not been tested yet. This notice goes away once the next real controller update has been verified." };
export const BUSY_LABELS = { checking: "Checking…", downloading: "Downloading…", installing: "Installing…", verifying: "Verifying…" };

/** The install confirmation: every newer component with its versions, then what to expect. */
export function installConfirm(status) {
  const changes = (status?.components || []).filter(c => c?.newer).map(c => `${c.label}: ${c.installed} → ${c.available}`).join("\n");
  return { title: "Install controller firmware?", ok: "Install",
    text: `${changes}\n\nThe update takes about 5 minutes. Keep AC power connected and do not detach the controllers. Controller input may be unavailable until it finishes.` };
}
