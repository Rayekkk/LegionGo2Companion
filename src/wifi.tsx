// SPDX-License-Identifier: BSD-3-Clause

import { callable, toaster, useQuickAccessVisible } from "@decky/api";
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { confirmAction, detailsRows, infoRow, statusPlaceholder } from "./shared";

interface WifiSettings {
  device_family?: string;
  device_label?: string;
  driver?: string;
  band_policy?: "off" | "five_six_no_24" | "five_six_only" | "six_ghz_only";
  band_preference_enabled?: boolean;
}

interface WifiLiveStatus {
  ssid?: string;
  connection_state?: "connected" | "connecting" | "disconnected" | "unknown";
  connection_uuid?: string;
  band_policy?: WifiSettings["band_policy"];
  saved_policy_inactive?: boolean;
  band_policy_restore_available?: boolean;
  six_ghz_only_supported?: boolean;
  signal_dbm?: string;
  tx_bitrate?: string;
  frequency?: string;
  channel?: string;
  wifi_backend?: string;
  high_band_discovery_pending?: boolean;
  band_policy_error?: string;
  recovery_required?: boolean;
  recovery_errors?: string[];
}

export interface WifiStatus {
  success: boolean;
  connected?: boolean;
  settings?: WifiSettings;
  live?: WifiLiveStatus;
  drift?: Record<string, boolean>;
  error?: string;
  message?: string;
}

interface WifiResult {
  success: boolean;
  error?: string;
  message?: string;
  detail?: string;
  reconnected?: boolean;
  frequency?: number | null;
}

export const getWifiStatus = callable<[], WifiStatus>("wifi_get_status");
const setBandPolicy = callable<[
  mode: "off" | "five_six_no_24" | "five_six_only" | "six_ghz_only",
  allowUnverifiedScan?: boolean,
], WifiResult>("wifi_set_band_policy");
const rescanAndReconnect = callable<[targetBand?: "high" | "six"], WifiResult>(
  "wifi_rescan_and_reconnect",
);

const bandName = (frequency?: string | number | null) => {
  const value = Number(frequency);
  if (!Number.isFinite(value) || value <= 0) return "Unknown band";
  if (value < 3000) return "2.4 GHz";
  if (value < 5925) return "5 GHz";
  return "6 GHz";
};

type Policy = NonNullable<WifiSettings["band_policy"]>;
const POLICY_LABELS: Record<Policy, string> = {
  off: "Automatic", five_six_no_24: "Prefer 5/6 GHz", five_six_only: "Only 5/6 GHz", six_ghz_only: "Only 6 GHz",
};
type WifiAction = "off" | "prefer" | "strict" | "high" | "six" | "sixOnly";
const POLICY_ACTIONS: Record<Policy, WifiAction> = { off: "off", five_six_no_24: "prefer", five_six_only: "strict", six_ghz_only: "sixOnly" };
const savedPolicy = (status: WifiStatus): Policy => status.settings?.band_policy ??
  (status.settings?.band_preference_enabled ? "five_six_no_24" : "off");
/** A saved policy the active backend cannot use (iwd choices under WPA, or 6 GHz only under iwd). */
const inactivePolicy = (status: WifiStatus) => {
  const backend = status.live?.wifi_backend, saved = savedPolicy(status);
  return (backend === "wpa_supplicant" && (saved === "five_six_no_24" || saved === "five_six_only"))
    || (backend === "iwd" && saved === "six_ghz_only");
};

export const wifiSummary = (status?: WifiStatus) => {
  if (!status?.success || !status.settings) return "Wi-Fi status unavailable";
  const backend = status.live?.wifi_backend;
  if (backend !== "iwd" && backend !== "wpa_supplicant") return "Wi-Fi backend unavailable";
  const inactive = inactivePolicy(status);
  const link = status.connected ? bandName(status.live?.frequency)
    : status.live?.connection_state === "connecting" ? "Connecting…" : "Not connected";
  return `${POLICY_LABELS[inactive ? "off" : savedPolicy(status)]}${inactive ? " · saved setting unused" : ""} · ${link}`;
};

const canRun = (status: WifiStatus | null, action: WifiAction) => {
  if (!status?.success || status.settings?.device_family !== "legion_go_2" || status.settings.driver !== "mt7921e") return false;
  const backend = status.live?.wifi_backend;
  if (backend !== "iwd" && backend !== "wpa_supplicant") return false;
  if (status.live?.connection_state === "unknown") return false;
  const policy = savedPolicy(status);
  if (action === "off") return status.live?.band_policy_restore_available === true ||
    (!status.live?.recovery_required && policy !== "off");
  if (policy === "six_ghz_only" || status.live?.recovery_required || status.live?.band_policy_error) return false;
  if (action === "prefer") return backend === "iwd" && policy !== "five_six_no_24";
  if (action === "strict") return backend === "iwd" && status.connected === true && policy !== "five_six_only";
  if (action === "sixOnly") return backend === "wpa_supplicant" && status.live?.six_ghz_only_supported === true && status.connected === true && policy === "off";
  return status.connected === true && (action === "six"
    ? backend === "wpa_supplicant" && policy === "off"
    : (backend === "iwd" && policy === "five_six_no_24") || (backend === "wpa_supplicant" && policy === "off"));
};

const resultMessage = (result: WifiResult) =>
  [result.message, result.detail].filter(Boolean).join(" — ") ||
  "The operation could not be verified.";
const notify = (body: string) => toaster.toast({ title: "Wi-Fi", body });

export const WifiPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const inFlight = useRef<Promise<WifiStatus> | null>(null);
  const refreshAfterRead = useRef(false);
  const operationInFlight = useRef(false);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
  const latestStatus = useRef<WifiStatus | null>(null);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);
  const [status, setStatus] = useState<WifiStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [details, setDetails] = useState(false);

  const refresh = useCallback(async (verifyOperation = false): Promise<WifiStatus | null> => {
    if (!mounted.current || !isVisible.current || inFlight.current || (operationInFlight.current && !verifyOperation)) return null;
    const revision = readRevision.current;
    const request = getWifiStatus();
    inFlight.current = request;
    try {
      const next = await request;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return null;
      latestStatus.current = next.success ? next : null;
      setStatus(next.success ? next : null);
      setStatusError(next.success ? "" : next.message ?? "Wi-Fi status is unavailable.");
      return next.success ? next : null;
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        latestStatus.current = null; setStatus(null);
        setStatusError("Wi-Fi status is unavailable. Retrying while this page is open.");
      }
      return null;
    } finally {
      if (inFlight.current === request) inFlight.current = null;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refresh(); }
    }
  }, []);

  useEffect(() => {
    if (!visible) {
      latestStatus.current = null; setStatus(null);
      setError(""); setStatusError("");
      return;
    }
    if (inFlight.current) refreshAfterRead.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); readRevision.current += 1; latestStatus.current = null; };
  }, [refresh, visible]);

  // The band menu and its confirmations take focus from the Quick Access menu, which clears the
  // status, so they act on the status they showed (`seen`) unless a newer read contradicts it.
  const run = useCallback(
    async (action: WifiAction, operation: () => Promise<WifiResult>, seen = latestStatus.current) => {
      if (!mounted.current || operationInFlight.current) return;
      const before = latestStatus.current ?? seen;
      if (!canRun(before, action)) {
        if (latestStatus.current && seen !== latestStatus.current) notify("Wi-Fi changed. Choose again.");
        return;
      }
      readRevision.current += 1;
      operationInFlight.current = true;
      setBusy(true);
      setError("");
      let result: WifiResult | undefined;
      const fail = (message: string) => { setError(message); notify(message); };
      try {
        result = await operation();
        if (!mounted.current) return;
        if (!result.success) fail(resultMessage(result));
      } catch {
        if (mounted.current) fail("The backend call ended before the result could be verified.");
      } finally {
        await inFlight.current?.catch(() => undefined);
        const verified = await refresh(true);
        if (mounted.current && isVisible.current && result?.success) {
          const policy = verified && savedPolicy(verified);
          const expected = action === "off" ? "off" : action === "prefer" ? "five_six_no_24" : action === "strict" ? "five_six_only" : action === "sixOnly" ? "six_ghz_only" : null;
          const unchanged = verified && before?.live?.wifi_backend === verified.live?.wifi_backend &&
            (!before?.live?.connection_uuid || before.live.connection_uuid === verified.live?.connection_uuid);
          const frequency = Number(verified?.live?.frequency);
          const applied = expected ? policy === expected : verified?.connected && frequency >= (action === "six" ? 5925 : 4900);
          if (unchanged && applied && !verified.live?.recovery_required && !verified.live?.band_policy_error)
            notify(resultMessage(result));
          else fail("The final connection or setting could not be verified. Check the current Wi-Fi state.");
        }
        operationInFlight.current = false;
        if (mounted.current) setBusy(false);
      }
    },
    [refresh],
  );

  if (!status) return statusPlaceholder({ label: "Wi-Fi status unavailable", error: statusError, onRetry: () => void refresh() });

  const settings = status.settings ?? {};
  const live = status.live ?? {};
  const policy = savedPolicy(status);
  const supported = settings.device_family === "legion_go_2" && settings.driver === "mt7921e";
  const frequency = live.frequency;
  const iwd = live.wifi_backend === "iwd";
  const wpa = live.wifi_backend === "wpa_supplicant";
  const inactive = inactivePolicy(status);
  const disabled = (action: WifiAction) => busy || !canRun(status, action);

  // Choices this backend offers, plus a saved one it does not, so that it can still be turned off.
  const offered: Policy[] = ["off", ...(iwd ? ["five_six_no_24", "five_six_only"] as Policy[] : []),
    ...(wpa && live.six_ghz_only_supported === true ? ["six_ghz_only"] as Policy[] : [])];
  if (!offered.includes(policy)) offered.push(policy);
  const options = offered.filter(id => id === policy || canRun(status, POLICY_ACTIONS[id]))
    .map(id => ({ data: id, label: POLICY_LABELS[id] }));
  const choose = (id: Policy, seen: WifiStatus) => {
    const known = latestStatus.current ?? seen;
    if (id === savedPolicy(known) || !canRun(known, POLICY_ACTIONS[id])) return;
    if (id === "five_six_only") confirmAction("Use only 5/6 GHz?",
      "Wi-Fi stops using 2.4 GHz and reconnects. If no 5/6 GHz network connects, your previous setting comes back automatically.",
      "Use only 5/6 GHz", () => void run("strict", () => setBandPolicy("five_six_only", true), known));
    else if (id === "six_ghz_only") confirmAction("Use only 6 GHz?",
      "Wi-Fi will connect only to 6 GHz networks, also after a restart. Away from a 6 GHz router you'll have no Wi-Fi until you choose Automatic.",
      "Use only 6 GHz", () => void run("sixOnly", () => setBandPolicy("six_ghz_only", false), known));
    else void run(POLICY_ACTIONS[id], () => setBandPolicy(id), known);
  };
  const band = bandName(frequency);
  const bandDescription = error || (busy ? "Applying…"
    : inactive ? "This saved setting isn't used with the current Wi-Fi backend. Choose Automatic to clear it."
    : policy === "five_six_only" && !status.connected ? "No 5/6 GHz network. Choose Automatic or Prefer 5/6 GHz."
    : policy === "five_six_only" && status.connected && band === "2.4 GHz" ? "Saved: Only 5/6 GHz, but connected on 2.4 GHz."
    : live.band_policy_error ? live.band_policy_error
    : policy === "off" ? (wpa ? "SteamOS picks the band and roams automatically." : "SteamOS picks the band.")
    : policy === "five_six_no_24" ? "Uses 2.4 GHz only when 5/6 GHz isn't available."
    : policy === "five_six_only" ? "Never uses 2.4 GHz." : "Connects only to 6 GHz networks.");

  return (
    <PanelSection title="Wi-Fi">
      {!supported ? infoRow("Wi-Fi tweaks unavailable", "Only for the Legion Go 2 Wi-Fi card (MT7922).")
        : live.recovery_required ? infoRow("Wi-Fi settings are locked",
          live.recovery_errors?.join("; ") || "A previous change must be recovered before another change.")
        : !iwd && !wpa && infoRow("Wi-Fi backend unavailable", "Wi-Fi controls appear once SteamOS reports its Wi-Fi backend.")}
      {infoRow(status.connected ? (live.ssid ?? "Connected") : live.connection_state === "connecting" ? "Connecting…" : "Not connected",
        busy ? "Checking the connection…" : status.connected
          ? `${band}${live.channel ? ` · channel ${live.channel}` : ""}${live.high_band_discovery_pending ? " · looking for 5/6 GHz" : ""}`
          : live.connection_state === "unknown" ? "Connection state unavailable." : undefined)}
      {(iwd || wpa) && <PanelSectionRow>
        <DropdownItem label="Band" description={bandDescription} selectedOption={policy} rgOptions={options}
          disabled={busy || options.length < 2} onChange={option => choose(option.data as Policy, status)} />
      </PanelSectionRow>}
      {((iwd && policy === "five_six_no_24") || (wpa && policy === "off")) && <PanelSectionRow>
        <ButtonItem layout="below" disabled={disabled("high")} onClick={() => void run("high", rescanAndReconnect)}>
          Reconnect to 5/6 GHz
        </ButtonItem>
      </PanelSectionRow>}
      {wpa && policy === "off" && <PanelSectionRow>
        <ButtonItem layout="below" description="Then roams normally again." disabled={disabled("six")}
          onClick={() => void run("six", () => rescanAndReconnect("six"))}>
          Connect to 6 GHz once
        </ButtonItem>
      </PanelSectionRow>}
      {detailsRows(details, () => setDetails(value => !value), [
        ["Backend", iwd ? "iwd" : wpa ? "WPA supplicant" : "Unavailable"],
        ["Driver", `${settings.driver ?? "unknown"}${settings.device_label ? ` · ${settings.device_label}` : ""}`],
        ["Frequency", status.connected && frequency ? `${frequency} MHz` : "—"],
      ])}
    </PanelSection>
  );
};
