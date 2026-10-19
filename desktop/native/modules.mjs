// SPDX-License-Identifier: BSD-3-Clause
// Module names and switch rules, shared by the Desktop app and the Decky panel (src/index.tsx imports this file).

// Key order is the menu and Manage Modules order.
export const MODULE_LABELS = { tdp: "TDP", fan: "Fan Control", vibration: "Vibration", rgb: "Lighting",
  remap: "Button mapping", controller: "Gyro & touchpad", battery: "Battery", display: "Display", wifi: "Wi-Fi" };
export const MODULE_DESCRIPTIONS = {
  tdp: "Power limits and CPU settings. Off: Lenovo's default limits return.",
  fan: "Your own fan curve. Off: the fan returns to automatic.",
  vibration: "Controller and touchpad vibration.",
  rgb: "Stick lights and power button light.",
  remap: "The two extra buttons on the left controller.",
  controller: "Gyro source and controller test.",
  battery: "Charge limit of about 80%.",
  display: "OLED brightness and HDR fixes. Changes need a Gaming Mode restart.",
  wifi: "Wi-Fi band preference.",
};

/** The switch shows the saved choice, so a module that failed to start reads On (with its error and
 *  a Retry … start) and can still be turned off. */
export const moduleChecked = entry => entry?.enabled === true;
/** Restoration and firmware maintenance finish first; cleanup has its own retry. */
export const moduleLocked = entry => !!entry?.pending || !!entry?.maintenance;
export const moduleNote = (name, entry) => entry?.error || entry?.note || MODULE_DESCRIPTIONS[name] || "";
export const cleanupRetryShown = entry => !!entry?.pending && !entry.maintenance;
export const startRetryShown = entry => entry?.enabled === true && entry.ready === false && !entry.pending && !entry.maintenance;
