// SPDX-License-Identifier: BSD-3-Clause
.pragma library
// Shared by Fan Control and Overview. A delayed sample is not current on arrival:
// its own age plus the transport age must stay short.
function readingFresh(readings, transportAge) {
    return !!readings && !!readings.valid && Number(readings.age_seconds || 0) + transportAge < 2.5;
}
function speedText(rpm, unit, maxRpm) {
    return unit === "percent" ? Math.round(100 * Number(rpm) / Number(maxRpm || 4800)) + "%" : Math.round(Number(rpm)) + " RPM";
}
function title(name) { const text = String(name || ""); return text.charAt(0).toUpperCase() + text.slice(1); }
// One status mapping for every Fan Control summary (UX spec 3.2), evaluated top-down.
// `starting` marks the transient state that turns into a retry hint after 30 s.
function status(state) {
    if (!state || typeof state !== "object" || !Object.keys(state).length) return {headline: "Reading fan…", note: ""};
    const settings = state.settings || {}, telemetry = state.telemetry, ownership = state.ownership || {};
    if (!state.supported) return {headline: "Not available on this console", note: ""};
    if (settings.enabled && state.error) return {headline: "Paused, resuming automatically", note: "No action needed."};
    if (settings.enabled && !(telemetry && telemetry.active)) return {headline: "Starting your curve…", note: "No action needed.", starting: true};
    if (!settings.enabled && state.error) return {headline: "Turned off after an error", note: state.error + " Turn on to try again."};
    if (!settings.enabled && ownership.blocked) return {headline: "Another app controls the fan", note: ownership.reason || ""};
    if (settings.enabled) return {headline: state.active_curve && state.active_curve !== "global" ? title(state.active_curve) + " TDP curve" : "Global curve",
        note: "Turn off to return the fan to automatic."};
    if (state.readings && state.readings.valid && state.readings.automatic === false) return {headline: "Another app controls the fan", note: ""};
    if (telemetry && !telemetry.automatic) return {headline: "Returning fan to automatic…", note: "No action needed."};
    return {headline: "Automatic", note: settings.assign_tdp_presets ? "Global curve with optional Companion TDP assignments." : "Uses your saved global curve."};
}
