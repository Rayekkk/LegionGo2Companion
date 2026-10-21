// SPDX-License-Identifier: BSD-3-Clause
// Module names and switch rules, shared by the Desktop app and the Decky panel (src/index.tsx imports this file).

// Key order is the menu and Manage Modules order.
export const MODULE_LABELS = { tdp: "TDP", fan: "Fan Control", vibration: "Vibration", rgb: "Lighting",
  remap: "Button mapping", controller: "Gyro & touchpad", battery: "Battery", display: "Display", wifi: "Wi-Fi" };
const MODULE_DESCRIPTIONS = {
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
/** A start still running past its time limit: the module waits in place of its controls, and Off is
 *  saved at once but applies when the start has finished. Restoration and maintenance read first. */
const MODULE_STARTING_TEXT = "Still starting. Its controls appear when it has finished.";
const MODULE_OFF_AFTER_START_TEXT = "Turns off when it has finished starting.";
// Turned off during a slow start, the module is also pending until the backend withdraws it.
export const moduleStartingText = entry => !entry?.starting || entry.maintenance ? ""
  : entry.enabled === false ? MODULE_OFF_AFTER_START_TEXT : MODULE_STARTING_TEXT;
/** What a module does on this console (get_version.device): the power button light only where it applies. */
const moduleDescription = (name, device) =>
  name === "rgb" && device?.power_led === false ? "Stick lights." : MODULE_DESCRIPTIONS[name] || "";
export const moduleNote = (name, entry, device) => moduleStartingText(entry) || entry?.error || entry?.note || moduleDescription(name, device);
/** A module that does not apply to this console is not shown anywhere, Manage Modules included.
 *  applicable null: the backend has not detected the console yet; no field: an older backend, shown. */
export const moduleShown = entry => entry?.applicable !== false && entry?.applicable !== null;
/** Which modules apply is not known yet (applicable null): the UI waits instead of flashing everything. */
export const consoleDetecting = modules => Object.values(modules ?? {}).some(entry => entry?.applicable === null);
export const cleanupRetryShown = entry => !!entry?.pending && !entry.maintenance && !entry.starting;
export const startRetryShown = entry => entry?.enabled === true && entry.ready === false && !entry.pending && !entry.maintenance && !entry.starting;

// Beta notices (Decky home page and Desktop Overview): consoles built from Lenovo's data but not yet tested.
const BETA_NOTICE_TEXT = "Built from Lenovo's firmware data, not yet tested on this console. Report problems on GitHub.";
const OLD_CONTROLLER_FIRMWARE_TEXT = "Update the controller firmware in Legion Space to unlock vibration, lighting and gyro.";
export const FAN_BETA_TEXT = "Beta on this model: the fan is checked by a self-test before Companion first takes control.";
const betaConsole = device => device?.console === "go1" ? "Legion Go" : device?.console === "gos" ? "Legion Go S" : "";
export const betaNoticeTitle = device => betaConsole(device) ? "Beta on " + betaConsole(device) : "";
/** A Go 1 whose controllers lack the kernel driver (device.controller_driver false) is on the older firmware. */
export const betaNoticeText = device => !betaConsole(device) ? ""
  : device.console === "go1" && device.controller_driver === false ? BETA_NOTICE_TEXT + " " + OLD_CONTROLLER_FIRMWARE_TEXT : BETA_NOTICE_TEXT;
