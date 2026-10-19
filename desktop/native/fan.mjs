// SPDX-License-Identifier: BSD-3-Clause
// Fan Control texts and curve editing, shared by the Desktop app and the Decky panel (src/fan.tsx imports this file).

export const STARTING_STUCK_MS = 30000;

export const curveName = name => name === "max" ? "Maximum" : String(name).charAt(0).toUpperCase() + String(name).slice(1);

/** The save button: the global curve, or the curve of the chosen TDP preset. */
export const saveLabel = scope => scope === "global" ? "Save global curve" : `Assign curve to ${curveName(scope)}`;

/** The selected point under the graph; the last point (100°C) only changes speed. */
export const pointText = (index, count, temperature, speed) =>
  `Point ${index + 1}/${count} · ${temperature}°C · ${speed}${index === count - 1 ? " · speed only" : ""}`;

// Desktop polling: a delayed sample is not current on arrival, so its own age plus the transport age must stay short.
export const readingFresh = (readings, transportAge) =>
  !!readings && !!readings.valid && Number(readings.age_seconds || 0) + transportAge < 2.5;

export function speedText(rpm, unit, maxRpm) {
  const percent = unit === "percent";
  if (rpm === undefined || rpm === null || !Number.isFinite(Number(rpm))) return percent ? "—%" : "— RPM";
  return percent ? `${Math.min(100, Math.max(0, Number(rpm) / Number(maxRpm || 4800) * 100)).toFixed(1)}%` : `${Math.round(Number(rpm))} RPM`;
}

/**
 * One status for every fan view, evaluated top-down from fields the backend
 * already returns. `startingFor` (ms) is how long the curve has been starting.
 */
export function fanStatus(state, startingFor = 0) {
  if (!state || typeof state !== "object" || !Object.keys(state).length) return { headline: "Reading fan…", detail: "" };
  const settings = state.settings || {}, telemetry = state.telemetry, ownership = state.ownership || {};
  if (!state.supported) return { headline: "Not available on this console", detail: state.error || "" };
  const enabled = settings.enabled;
  // Pause/retry keeps Fan Control on and reports why; a latched fault turns it off.
  if (enabled && state.error) return { headline: "Paused, resuming automatically", detail: "No action needed." };
  if (enabled && !telemetry?.active) return { headline: "Starting your curve…", starting: true, detail: startingFor >= STARTING_STUCK_MS
    ? "Still starting. Turn Fan Control off and on to retry." : "No action needed." };
  if (!enabled && state.error) return { headline: "Turned off after an error", detail: `${state.error} Turn on to try again.` };
  if (!enabled && ownership.blocked) return { headline: "Another app controls the fan", detail: ownership.reason || "" };
  if (enabled) return { headline: !state.active_curve || state.active_curve === "global" ? "Global curve" : `${curveName(state.active_curve)} TDP curve`,
    detail: "Turn off to return the fan to automatic." };
  if (state.readings?.valid && state.readings.automatic === false) return { headline: "Another app controls the fan", detail: "" };
  if (telemetry && !telemetry.automatic) return { headline: "Returning fan to automatic…", detail: "No action needed." };
  return { headline: "Automatic", detail: settings.assign_tdp_presets
    ? "Global curve with optional Companion TDP assignments." : "Uses your saved global curve." };
}

/** Set one axis of one point. A speed between 0 and the minimum running speed snaps to the nearer of the two. */
export function editPoint(curve, index, axis, value, limits) {
  const next = curve.map(([t, r]) => [t, r]);
  const last = index === curve.length - 1;
  if (!Number.isFinite(value) || !Number.isInteger(index) || index < 0 || index >= curve.length || last && axis === 0) return next;
  if (axis === 0) {
    next[index][0] = Math.max(index ? curve[index - 1][0] + 1 : 0, Math.min(curve[index + 1][0] - 1, Math.round(value)));
  } else {
    let rpm = Math.round(value / limits.step) * limits.step;
    if (rpm > 0 && rpm < limits.min_rpm) rpm = rpm >= limits.min_rpm / 2 ? limits.min_rpm : 0;
    // The 100°C point stays at 100°C and never runs below its own floor.
    next[index][1] = Math.max(index ? curve[index - 1][1] : 0, last ? limits.last_min_rpm : 0, Math.min(last ? limits.max_rpm : curve[index + 1][1], limits.max_rpm, rpm));
  }
  return next;
}

/** A speed step from a button or key: up from a stopped fan starts at the minimum, down below it stops. */
export function nudgeSpeed(rpm, delta, minRpm) {
  const next = rpm + delta;
  return delta > 0 && rpm < minRpm ? Math.max(next, minRpm) : delta < 0 && next < minRpm ? 0 : next;
}
