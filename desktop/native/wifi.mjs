// SPDX-License-Identifier: BSD-3-Clause
// Wi-Fi rules and texts, shared by the Desktop app and the Decky panel (src/wifi.tsx imports this file).

const POLICY_LABELS = { off: "Automatic", five_six_no_24: "Prefer 5/6 GHz", five_six_only: "Only 5/6 GHz", six_ghz_only: "Only 6 GHz" };
/** The action that applies each band policy; "high" and "six" are the one-time reconnects. */
export const POLICY_ACTIONS = { off: "off", five_six_no_24: "prefer", five_six_only: "strict", six_ghz_only: "sixOnly" };
export const CONFIRMATIONS = {
  five_six_only: { title: "Use only 5/6 GHz?", ok: "Use only 5/6 GHz",
    text: "Wi-Fi stops using 2.4 GHz and reconnects. If no 5/6 GHz network connects, your previous setting comes back automatically." },
  six_ghz_only: { title: "Use only 6 GHz?", ok: "Use only 6 GHz",
    text: "Wi-Fi will connect only to 6 GHz networks, also after a restart. Away from a 6 GHz router you'll have no Wi-Fi until you choose Automatic." },
};
export const CHANGED = "Wi-Fi changed. Choose again.";
export const UNVERIFIED = "The final connection or setting could not be verified. Check the current Wi-Fi state.";

function bandName(frequency) {
  const value = Number(frequency);
  if (!Number.isFinite(value) || value <= 0) return "Unknown band";
  return value < 3000 ? "2.4 GHz" : value < 5925 ? "5 GHz" : "6 GHz";
}

export const savedPolicy = status => status?.settings?.band_policy ?? "off";
// A status read the backend answered (a refusal has no settings, and neither does the Desktop app before its first read).
const read = status => !!status?.settings;
const backendOf = status => { const b = status?.live?.wifi_backend; return b === "iwd" || b === "wpa_supplicant" ? b : ""; };
// Every Legion Go (wifi_backend.LEGION_GO_FAMILIES) with the MT7922 driver.
const supported = status => ["legion_go", "legion_go_s", "legion_go_2"].includes(status?.settings?.device_family) && status.settings.driver === "mt7921e";

/** A saved policy the active backend cannot use (iwd choices under WPA, or 6 GHz only under iwd). */
function inactivePolicy(status) {
  const backend = status?.live?.wifi_backend, saved = savedPolicy(status);
  return (backend === "wpa_supplicant" && (saved === "five_six_no_24" || saved === "five_six_only"))
    || (backend === "iwd" && saved === "six_ghz_only");
}

/** Whether an action may start now. Automatic stays available whenever there is something to restore. */
export function canRun(status, action) {
  if (!read(status)) return false;
  const backend = backendOf(status), live = status.live || {}, policy = savedPolicy(status);
  if (!backend) return false;
  // Automatic only removes Companion's own change; the backend recovers or refuses it.
  if (action === "off") return live.band_policy_restore_available === true || policy !== "off";
  if (!supported(status) || live.connection_state === "unknown") return false;
  if (policy === "six_ghz_only" || live.recovery_required || live.band_policy_error) return false;
  const connected = status.connected === true;
  if (action === "prefer") return backend === "iwd" && policy !== "five_six_no_24";
  if (action === "strict") return backend === "iwd" && connected && policy !== "five_six_only";
  if (action === "sixOnly") return backend === "wpa_supplicant" && live.six_ghz_only_supported === true && connected && policy === "off";
  if (action === "six") return connected && backend === "wpa_supplicant" && policy === "off";
  return action === "high" && connected && (backend === "iwd" ? policy === "five_six_no_24" : policy === "off");
}

/** Whether the Band choice or a reconnect button is shown at all ("band", "high" or "six"). */
export function actionShown(status, action) {
  const backend = backendOf(status), policy = savedPolicy(status);
  if (action === "band") return !!backend;
  if (action === "six") return backend === "wpa_supplicant" && policy === "off";
  return (backend === "iwd" && policy === "five_six_no_24") || (backend === "wpa_supplicant" && policy === "off");
}

/** Choices this backend offers that can run now, plus the saved one so it stays selected. */
export function bandOptions(status) {
  const backend = backendOf(status), policy = savedPolicy(status);
  const offered = ["off", ...(backend === "iwd" ? ["five_six_no_24", "five_six_only"] : []),
    ...(backend === "wpa_supplicant" && status?.live?.six_ghz_only_supported === true ? ["six_ghz_only"] : [])];
  if (!offered.includes(policy)) offered.push(policy);
  return offered.filter(id => id === policy || canRun(status, POLICY_ACTIONS[id])).map(id => ({ id, label: POLICY_LABELS[id] || id }));
}

/** The connected band a saved policy excludes, or "". */
function strayBand(status) {
  if (!status?.connected) return "";
  const band = bandName(status.live?.frequency), policy = savedPolicy(status);
  return (policy === "five_six_only" && band === "2.4 GHz") || (policy === "six_ghz_only" && (band === "2.4 GHz" || band === "5 GHz")) ? band : "";
}

/** The note under Band (each UI shows its own operation error and "Applying…" first). */
export function bandNote(status) {
  const live = status?.live || {}, policy = savedPolicy(status), stray = strayBand(status);
  if (inactivePolicy(status)) return "This saved setting isn't used with the current Wi-Fi backend. Choose Automatic to clear it.";
  if (policy === "five_six_only" && !status?.connected) return "No 5/6 GHz network. Choose Automatic or Prefer 5/6 GHz.";
  if (stray) return `Saved: ${POLICY_LABELS[policy]}, but connected on ${stray}.`;
  if (live.band_policy_error) return live.band_policy_error;
  return ({ off: live.wifi_backend === "wpa_supplicant" ? "SteamOS picks the band and roams automatically." : "SteamOS picks the band.",
    five_six_no_24: "Uses 2.4 GHz only when 5/6 GHz isn't available.", five_six_only: "Never uses 2.4 GHz.",
    six_ghz_only: "Connects only to 6 GHz networks." })[policy] || "";
}

/** Why Wi-Fi changes are unavailable, as [title, detail], or null. */
export function wifiNotice(status) {
  if (!read(status)) return null;
  const live = status.live || {};
  if (!supported(status)) return ["Wi-Fi tweaks unavailable", "Only for the Legion Go Wi-Fi card (MT7922)."];
  if (live.recovery_required) return ["Wi-Fi settings are locked",
    (live.recovery_errors || []).join("; ") || "A previous change must be recovered before another change."];
  if (!backendOf(status)) return ["Wi-Fi backend unavailable", "Wi-Fi controls appear once SteamOS reports its Wi-Fi backend."];
  return null;
}

export const networkName = status => status?.connected ? status.live?.ssid || "Connected"
  : status?.live?.connection_state === "connecting" ? "Connecting…" : "Not connected";

export function linkText(status) {
  const live = status?.live || {};
  if (status?.connected) return `${bandName(live.frequency)}${live.channel ? ` · channel ${live.channel}` : ""}${live.high_band_discovery_pending ? " · looking for 5/6 GHz" : ""}`;
  return live.connection_state === "unknown" ? "Connection state unavailable." : "";
}

export const backendName = status => ({ iwd: "iwd", wpa_supplicant: "WPA supplicant" })[backendOf(status)] || "Unavailable";
export const driverText = status => `${status?.settings?.driver ?? "unknown"}${status?.settings?.device_label ? ` · ${status.settings.device_label}` : ""}`;
export const frequencyText = status => status?.connected && status.live?.frequency ? `${status.live.frequency} MHz` : "—";

export function wifiSummary(status) {
  if (!read(status)) return "Wi-Fi status unavailable";
  if (!backendOf(status)) return "Wi-Fi backend unavailable";
  const inactive = inactivePolicy(status);
  const link = status.connected ? bandName(status.live?.frequency)
    : status.live?.connection_state === "connecting" ? "Connecting…" : "Not connected";
  return `${POLICY_LABELS[inactive ? "off" : savedPolicy(status)]}${inactive ? " · saved setting unused" : ""} · ${link}`;
}

/** Whether a successful action is visible in the status read after it, on the same backend and network. */
export function operationVerified(action, before, after) {
  const live = after?.live || {};
  if (!read(after) || before?.live?.wifi_backend !== live.wifi_backend) return false;
  if (before?.live?.connection_uuid && before.live.connection_uuid !== live.connection_uuid) return false;
  if (live.recovery_required || live.band_policy_error) return false;
  const expected = Object.keys(POLICY_ACTIONS).find(id => POLICY_ACTIONS[id] === action);
  if (expected) return savedPolicy(after) === expected;
  return !!after.connected && Number(live.frequency) >= (action === "six" ? 5925 : 4900);
}
