// SPDX-License-Identifier: BSD-3-Clause
// Vibration names and rules, shared by the Desktop app and the Decky panel (src/vibration.tsx imports this file).

/** Strength names by level 0–3; the Decky slider notches use the short form, which fits the panel width. */
export const LEVELS = ["Off", "Low", "Medium", "High"];
export const LEVELS_SHORT = ["Off", "Low", "Med", "High"];
export const FALLBACK_MODES = ["fps", "racing", "standard", "spg", "rpg"];

// A mode the driver adds later is title-cased, so it shows up without a plugin release.
const MODE_NAMES = { fps: "FPS", racing: "Racing", standard: "Standard", spg: "SPG", rpg: "RPG" };
const MODE_SHORT = { racing: "Race", standard: "Std" };
export const modeLabel = raw => MODE_NAMES[raw] ?? String(raw).charAt(0).toUpperCase() + String(raw).slice(1);
export const modeShort = raw => MODE_SHORT[raw] ?? modeLabel(raw).slice(0, 5);

/** [title, detail] while vibe_get_driver_status does not report the hid-lenovo-go driver; null once found. */
export const driverNotice = status => status?.found === true ? null : ["Controller driver not found", "Needs SteamOS 3.8 or newer."];

/** Touchpad strength only applies while touchpad vibration is on. */
export const touchpadStrengthEditable = settings => settings?.touchpadEnabled === true;

/** Backend reply to a change made while no controller is connected: the change is kept and applied once it connects. */
const NOT_CONNECTED = "Controller not connected. The change is saved and applies when it connects.";
/** Message for a failed change. A saved change waiting for the controller reads exactly as in the Desktop app, without "<what>: ". */
export const failureText = (what, error) => error === NOT_CONNECTED ? error : `${what}: ${error || "The driver rejected the change"}`;
