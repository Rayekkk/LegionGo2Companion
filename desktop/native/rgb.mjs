// SPDX-License-Identifier: BSD-3-Clause
// Lighting rules and texts, shared by the Desktop app and the Decky panel (src/rgb.tsx imports this file).

const RESTORING = "Restoring saved state…";
export const RESTORE_NOTE = "Returns the lights to how they were before Companion.";
export const SPEEDS = [{ id: 0, label: "Slow" }, { id: 50, label: "Medium" }, { id: 100, label: "Fast" }];

/** Effect, colour, brightness and speed only mean something while lighting control and the stick lights are on. */
export const effectsEditable = settings => !!settings?.control_enabled && !!settings.rings_enabled;
export const usesColor = settings => settings?.effect === "monocolor" || settings?.effect === "breathe";
export const usesSpeed = settings => settings?.effect !== "monocolor";
export const speedStep = speed => speed <= 25 ? 0 : speed >= 75 ? 100 : 50;

export const controlNote = status => status?.rgb?.supported === true
  ? "Companion sets the stick lights and restores them after sleep."
  : `Not available: ${status?.rgb?.reason ?? "the controller lights were not detected."}`;
export const ringsNote = status => status?.rgb?.drift ? RESTORING : "";

/** The saved choice once Companion manages the power button light, otherwise what the light shows. */
export const powerChecked = status => status?.settings?.power_led_managed
  ? !!status.settings.power_led_enabled : status?.power_led?.current ?? true;
/** Shown beside the switch where the backend calls the light a beta (power_led.beta: Legion Go, Go S). */
export const POWER_LED_BETA_TEXT = "Beta on this console: the power-button light is set through the same firmware field as on the Legion Go 2, checked against this console's BIOS before Companion uses it.";
export const powerNote = status => status?.power_led?.supported
  ? `Turn the light in the power button on or off.${status.power_led.drift ? ` ${RESTORING}` : ""}`
  : status?.power_led?.reason ?? "The power button light is unavailable.";
