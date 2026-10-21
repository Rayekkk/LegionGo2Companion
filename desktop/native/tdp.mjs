// SPDX-License-Identifier: BSD-3-Clause
// TDP and CPU rules, texts and the Custom slider model, shared by the Desktop app and the Decky panel
// (src/tdp.tsx imports this file).

// ── Custom slider model ───────────────────────────────────────────────────────
// SPL is the dial; slow and fast boost are headroom above it, so raising the TDP carries them along and no
// slider position breaks FPPT >= SPPT >= SPL or the ceilings. A tuning is {spl, spptOff, fpptOff} in watts;
// caps are {spl, sppt, fppt}.
const OFFSET_MAX = { sppt: 15, fppt: 15 };
// Placeholders until get_caps() answers with this console's firmware ceilings (the Legion Go 2's numbers,
// FALLBACK_STD_W in tdp_backend.py). Nothing after that answer uses them.
export const FALLBACK_STD = { spl: 35, sppt: 37, fppt: 45 };
// With Extras only the boosts go higher; the firmware keeps sustained power (SPL) at its own ceiling.
export const FALLBACK_MAX = { spl: 35, sppt: 50, fppt: 50 };
export const FALLBACK_MIN = 5;

const clamp = (v, lo, hi) => Math.min(Math.max(v, lo), hi);

/** Headroom still available above the current SPL, per parameter. */
export const offsetMax = (spl, caps) => ({
  sppt: Math.max(0, Math.min(OFFSET_MAX.sppt, caps.sppt - spl)),
  fppt: Math.max(0, Math.min(OFFSET_MAX.fppt, caps.fppt - spl)),
});

/** Force a tuning back inside the ceilings, keeping SPPT <= FPPT. */
export function normalise(t, caps, minW) {
  const spl = clamp(t.spl, minW, caps.spl), max = offsetMax(spl, caps);
  const spptOff = clamp(t.spptOff, 0, max.sppt);
  return { spl, spptOff, fpptOff: Math.max(clamp(t.fpptOff, 0, max.fppt), spptOff) };
}

export const absolute = t => ({ spl: t.spl, sppt: t.spl + t.spptOff, fppt: t.spl + t.fpptOff });
export const fromAbsolute = (spl, sppt, fppt) => ({ spl, spptOff: Math.max(0, sppt - spl), fpptOff: Math.max(0, fppt - spl) });

/** Standard single-dial Custom mode uses the range in force: the firmware's, or the Extras range. */
export const simpleCaps = caps => caps;

export function simpleTuning(spl, caps, minW) {
  const bounded = clamp(spl, minW, caps.spl);
  return fromAbsolute(bounded, Math.min(bounded + 10, caps.sppt), Math.min(bounded + 15, caps.fppt));
}

/** Moving SPL re-clamps both offsets: at the ceiling there is no headroom left. */
export const withSpl = (t, v, caps, minW) => normalise({ spl: v, spptOff: t.spptOff, fpptOff: t.fpptOff }, caps, minW);

/** Pushing SPPT up drags FPPT along so SPPT never overtakes it. */
export function withSppt(t, v, caps) {
  const spptOff = clamp(v, 0, offsetMax(t.spl, caps).sppt);
  return { spl: t.spl, spptOff, fpptOff: Math.max(t.fpptOff, spptOff) };
}

/** Pulling FPPT below SPPT drags SPPT down to meet it. */
export function withFppt(t, v, caps) {
  const fpptOff = clamp(v, 0, offsetMax(t.spl, caps).fppt);
  return { spl: t.spl, spptOff: Math.min(t.spptOff, fpptOff), fpptOff };
}

// ── Presets ───────────────────────────────────────────────────────────────────
// Used until get_caps() answers. The backend remains the source of truth.
export const PRESETS = {
  silent: { spl: 8, sppt: 15, fppt: 20 },
  balanced: { spl: 16, sppt: 25, fppt: 30 },
  performance: { spl: 20, sppt: 32, fppt: 35 },
  max: { spl: 35, sppt: 37, fppt: 45 },
};
export const PRESET_LABELS = { silent: "Silent", balanced: "Balanced", performance: "Performance", max: "Maximum", custom: "Custom" };
export const PRESET_ORDER = ["silent", "balanced", "performance", "max", "custom"];

function detectPreset(spl, sppt, fppt, table = PRESETS) {
  for (const key of Object.keys(table)) {
    const v = table[key];
    if (v.spl === spl && v.sppt === sppt && v.fppt === fppt) return key;
  }
  return "custom";
}

/** The saved preset name while its values still match, otherwise the preset these values are, or Custom. */
export function resolvedPreset(spl, sppt, fppt, stored, table) {
  if (stored === "custom") return "custom";
  const value = table[stored];
  return value && value.spl === spl && value.sppt === sppt && value.fppt === fppt ? stored : detectPreset(spl, sppt, fppt, table);
}

const exceedsCaps = (spl, sppt, fppt, caps) => spl > caps.spl || sppt > caps.sppt || fppt > caps.fppt;

/** What runs now according to get_settings (mW), in watts with its preset label; null while unknown. */
export function activeLimits(settings, table = PRESETS) {
  const spl = settings?.active_spl ?? settings?.spl, sppt = settings?.active_sppt ?? settings?.sppt;
  const fppt = settings?.active_fppt ?? settings?.fppt;
  if (spl == null || sppt == null || fppt == null) return null;
  // Battery values (Separate battery presets) are named by the backend, as their values are not the preset's.
  const battery = PRESET_LABELS[settings.battery_preset] ? settings.battery_preset : "";
  const toW = value => Math.round(value / 1000);
  // The saved name is Global's; a game's limits are named by their values.
  const global = (spl === settings.spl && sppt === settings.sppt && fppt === settings.fppt) || (!!battery
    && resolvedPreset(toW(settings.spl), toW(settings.sppt), toW(settings.fppt), settings.active_preset, table) === battery);
  const w = toW(spl), sw = toW(sppt), fw = toW(fppt);
  return { spl: w, sppt: sw, fppt: fw, global,
    label: PRESET_LABELS[battery || resolvedPreset(w, sw, fw, global ? settings.active_preset : undefined, table)] };
}

// ── Separate battery presets ──────────────────────────────────────────────────
export const BATTERY_PRESETS_LABEL = "Separate battery presets";
export const BATTERY_PRESETS_NOTE = "Silent, Balanced and Performance use the lower limits the firmware sets on battery.";
/** The preset values for settings that run on battery (`onBattery`) or on the charger. */
export const shownPresets = (table, batteryTable, separate, onBattery) =>
  separate && onBattery && batteryTable ? batteryTable : table;

/** The TDP summary on the overview (Decky's All Controls row and the Desktop badge). */
export function overviewSummary(settings, table = PRESETS) {
  if (settings?.enabled === false) return "Off · Lenovo defaults";
  const now = activeLimits(settings, table);
  return now ? `${now.label} · ${now.spl} W` : "Custom";
}

/** The note under the selected preset. */
export const presetNote = (key, table) => key === "custom" || !table[key] ? "Set your own limits below."
  : `${PRESET_LABELS[key]} · ${table[key].spl} W (boost ${table[key].sppt}/${table[key].fppt} W)`;

// ── Power texts ───────────────────────────────────────────────────────────────
/** The note under "Enable TDP control": who sets the limits, the limits now, the draw and the warning
 *  for limits above Lenovo's while 50 W is not allowed. `now` is {spl, sppt, fppt} in watts. */
export function powerSummary(enabled, owner, label, now, draw, unlocked, std) {
  if (!enabled) return "Off · the console uses Lenovo's power limits.";
  return `${owner}: ${label} · ${now.spl} W${draw == null ? "" : ` · drawing ${Number(draw).toFixed(1)} W`}${
    !unlocked && exceedsCaps(now.spl, now.sppt, now.fppt, std) ? " · above Lenovo's limit" : ""}`;
}

/** The note under "Settings for <game>"; use is "global", "game" or "charger". */
export const scopeNote = (use, name) => use === "global" ? `${name} uses Global settings, shared by all games.`
  : use === "game" ? `Only ${name} uses these settings.` : `${name} has separate settings on battery and on charger.`;

export const sourceNote = (acOnline, charger) => `${acOnline ? "Plugged in now." : "On battery now."}${charger === acOnline ? ""
  : charger ? " Changes apply when you plug in." : " Changes apply when you unplug."}`;

export const boostRoomNote = (spl, offset, room) => room === 0 ? "No room left at this power." : `Up to ${spl + offset} W.`;

/** What get_caps says about the TDP providers and the 50 W helper. */
export function capsState(caps) {
  const c = caps || {}, extras = c.extras !== false;
  const available = c.tdp_available ?? (c.wmi === undefined && c.extras === undefined ? true : c.wmi === true || c.extras === true);
  return { tdpAvailable: available, extrasAvailable: extras, helperRetryAvailable: c.helper_retry_available ?? !extras,
    tdpError: c.tdp_error || (available ? "" : "Neither the firmware TDP interface nor the verified RyzenAdj helper is available.") };
}

/** Why the limits are not what the settings say, as [title, detail], or null. */
export function tdpNotice(state, unlocked) {
  if (!state.tdpAvailable) return ["TDP control unavailable", state.tdpError];
  if (!state.extrasAvailable && unlocked) return ["50 W paused", "Firmware limits are active until the 50 W helper is available. Saved profiles are kept."];
  return null;
}

/** Whether "Allow up to 50 W" is offered at all. */
export const extrasShown = state => state.extrasAvailable || state.helperRetryAvailable;
/** A helper download only helps while TDP or the 50 W unlock is missing it. */
export const helperRetryShown = (state, unlocked) => state.helperRetryAvailable && (!state.tdpAvailable || (!state.extrasAvailable && unlocked));

export const extrasNote = (unlocked, state, std) => unlocked
  ? `Boosts go up to ${FALLBACK_MAX.fppt} W; sustained power stays at ${std.spl} W, the firmware's limit. Turning off lowers boosts above ${std.sppt}/${std.fppt} W.`
  : state.extrasAvailable ? "Beyond Lenovo's limits. Hotter, louder, shorter battery." : "Downloads a verified helper from GitHub first.";

export const extrasConfirm = state => ({ title: `Allow up to ${FALLBACK_MAX.fppt} W?`, ok: `Allow ${FALLBACK_MAX.fppt} W`,
  text: "This goes beyond Lenovo's power limits. The console runs hotter and louder, and the battery drains faster."
    + (state.extrasAvailable ? "" : " Companion first downloads the verified RyzenAdj helper from GitHub.") });

const wattText = value => value == null ? "–" : Number(value).toFixed(0);
/** The "Limits now" readout from get_tdp_info. */
export const limitsText = info => info?.success
  ? `SPL ${wattText(info.values?.spl_limit)} · SPPT ${wattText(info.values?.sppt_limit)} · FPPT ${wattText(info.values?.fppt_limit)} W` : "—";
/** The "Set by" readout from get_tdp_info. */
export const sourceText = info => (info?.success && info.values?.source) || "—";

// ── CPU ───────────────────────────────────────────────────────────────────────
export const EPP_NAMED_VALUES = { performance: 0, balance_performance: 128, balance_power: 191, power: 255 };
export const EPP_PREFERENCE = "Left: faster CPU. Right: more power left for the GPU.";

export function eppLabel(profile) {
  if (!profile) return "Unknown";
  const labels = { default: "System default", performance: "Prefer CPU", balance_performance: "Balanced · Prefer CPU",
    balance_power: "Balanced · Prefer GPU", power: "Prefer GPU" };
  if (labels[profile]) return labels[profile];
  const readable = String(profile).replace(/[-_]+/g, " ").trim();
  return readable.charAt(0).toUpperCase() + readable.slice(1);
}

function parseNumericEpp(value) {
  if (!value || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 0 && parsed <= 255 ? parsed : null;
}

/** An inactive profile shows its saved CPU target; hardware readback stays authoritative for the active one. */
export function editorControls(controls) {
  const profile = controls?.profile;
  if (!profile || profile.active) return controls;
  return Object.assign({}, controls, {
    cpu_boost: Object.assign({}, controls.cpu_boost, { enabled: profile.cpu_boost_enabled }),
    epp: Object.assign({}, controls.epp, { value: profile.epp, numeric_value: parseNumericEpp(profile.epp) }),
  });
}

/** The slider position in percent, rounded to the UI's step; 50% while the system sets the value. */
export function eppPercent(epp, step) {
  const named = epp?.value ? EPP_NAMED_VALUES[epp.value] : undefined;
  const raw = epp?.numeric_value ?? parseNumericEpp(epp?.value) ?? named ?? 128;
  return clamp(Math.round(raw * 100 / 255 / step) * step, 0, 100);
}
export const eppValue = percent => String(Math.round(clamp(percent, 0, 100) * 255 / 100));

export const eppTitle = epp => epp?.numeric_supported && parseNumericEpp(epp.value) != null
  ? "CPU energy preference" : `CPU energy preference · ${eppLabel(epp?.value)}`;
export const eppUnavailable = epp => epp?.error || "Not supported on this device.";

/** The note under the numeric energy slider (each UI shows its own errors and "Applying…" first). */
export function eppNote(epp) {
  if (!epp?.available) return eppUnavailable(epp);
  const systemSet = parseNumericEpp(epp.value) == null && (epp.value == null || EPP_NAMED_VALUES[epp.value] == null);
  return systemSet ? "Set by the system. Move the slider to choose." : EPP_PREFERENCE;
}

/** CPU Boost can change only while it is supported and its state could be read. */
export const boostUsable = boost => !!boost?.available && boost.enabled != null;
export const boostNote = boost => !boost?.available ? boost?.error || "Not supported on this device."
  : boost.enabled == null ? boost.error || "The current state could not be read."
  : boost.enabled ? "Higher CPU clocks; uses more power." : "CPU stays at base clock; saves power.";
