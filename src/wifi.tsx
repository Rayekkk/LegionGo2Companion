// SPDX-License-Identifier: BSD-3-Clause

import { callable, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  Spinner,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";

interface WifiSettings {
  device_family?: string;
  device_label?: string;
  driver?: string;
  chip_label?: string;
  band_policy?: "off" | "five_six_no_24" | "five_six_only" | "six_ghz_only";
  band_preference_enabled?: boolean;
  band_policy_legacy_detected?: boolean;
}

interface WifiLiveStatus {
  ssid?: string;
  connection_state?: "connected" | "connecting" | "disconnected" | "unknown";
  connection_uuid?: string;
  band_policy?: WifiSettings["band_policy"];
  saved_policy_inactive?: boolean;
  band_policy_restore_available?: boolean;
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
  mode: "off" | "five_six_no_24" | "five_six_only",
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

export const wifiSummary = (status?: WifiStatus) => {
  if (!status?.success || !status.settings) return "WiFi status unavailable";
  const backend = status.live?.wifi_backend;
  if (backend !== "iwd" && backend !== "wpa_supplicant") return "WiFi backend unavailable";
  const saved = status.settings.band_policy ??
    (status.settings.band_preference_enabled ? "five_six_no_24" : "off");
  const inactiveIwd = backend !== "iwd" && (saved === "five_six_no_24" || saved === "five_six_only");
  const inactiveSix = backend !== "wpa_supplicant" && saved === "six_ghz_only";
  const inactive = inactiveIwd || inactiveSix;
  const policy = inactive ? "off" : saved;
  const label = status.settings.band_policy_legacy_detected ? "Legacy 5 GHz setting"
    : policy === "six_ghz_only" ? "6 GHz only"
    : policy === "five_six_only" ? "5/6 GHz only"
    : policy === "five_six_no_24" ? "5/6 GHz preferred" : "Automatic WiFi";
  return `${label}${inactive ? ` · saved ${inactiveIwd ? "iwd" : "6 GHz"} setting inactive` : ""} · ${
    status.connected ? bandName(status.live?.frequency) : status.live?.connection_state ?? "disconnected"}`;
};

type WifiAction = "off" | "prefer" | "strict" | "high" | "six";
const savedPolicy = (status: WifiStatus) => status.settings?.band_policy ??
  (status.settings?.band_preference_enabled ? "five_six_no_24" : "off");
const connectionIdentity = (status: WifiStatus | null) =>
  JSON.stringify([status?.live?.wifi_backend, status?.live?.connection_uuid,
    status?.live?.connection_state ?? status?.connected, status && savedPolicy(status),
    status?.live?.frequency, status?.live?.ssid]);
const canRun = (status: WifiStatus | null, action: WifiAction) => {
  if (!status?.success || status.settings?.device_family !== "legion_go_2" || status.settings.driver !== "mt7921e") return false;
  const backend = status.live?.wifi_backend;
  if (backend !== "iwd" && backend !== "wpa_supplicant") return false;
  if (status.live?.connection_state === "unknown") return false;
  const policy = savedPolicy(status);
  const legacy = status.settings.band_policy_legacy_detected === true;
  if (action === "off") return status.live?.band_policy_restore_available === true ||
    (!status.live?.recovery_required && (policy !== "off" || legacy));
  if (legacy || policy === "six_ghz_only" || status.live?.recovery_required || status.live?.band_policy_error) return false;
  if (action === "prefer") return backend === "iwd" && policy !== "five_six_no_24";
  if (action === "strict") return backend === "iwd" && status.connected === true && policy !== "five_six_only";
  return status.connected === true && (action === "six"
    ? backend === "wpa_supplicant" && policy === "off"
    : (backend === "iwd" && policy === "five_six_no_24") || (backend === "wpa_supplicant" && policy === "off"));
};

const resultMessage = (result: WifiResult) =>
  [result.message, result.detail].filter(Boolean).join(" — ") ||
  "The operation could not be verified.";

export const WifiPage: FC = () => {
  const visible = useQuickAccessVisible();
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
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [confirmStrict, setConfirmStrict] = useState(false);

  const refresh = useCallback(async (verifyOperation = false): Promise<WifiStatus | null> => {
    if (!mounted.current || !isVisible.current || inFlight.current || (operationInFlight.current && !verifyOperation)) return null;
    const revision = readRevision.current;
    const request = getWifiStatus();
    inFlight.current = request;
    try {
      const next = await request;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return null;
      if (!next.success || connectionIdentity(next) !== connectionIdentity(latestStatus.current)) {
        setNotice(""); setConfirmStrict(false);
      }
      latestStatus.current = next.success ? next : null;
      setStatus(next.success ? next : null);
      setStatusError(next.success ? "" : next.message ?? "WiFi status is unavailable.");
      return next.success ? next : null;
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        latestStatus.current = null; setStatus(null); setNotice(""); setConfirmStrict(false);
        setStatusError("WiFi status is unavailable. Retrying while this page is open.");
      }
      return null;
    } finally {
      if (inFlight.current === request) inFlight.current = null;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refresh(); }
    }
  }, []);

  useEffect(() => {
    if (!visible) {
      latestStatus.current = null; setStatus(null); setNotice("");
      setError(""); setStatusError(""); setConfirmStrict(false);
      return;
    }
    if (inFlight.current) refreshAfterRead.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); readRevision.current += 1; latestStatus.current = null; };
  }, [refresh, visible]);

  const run = useCallback(
    async (action: WifiAction, operation: () => Promise<WifiResult>) => {
      if (!mounted.current || !isVisible.current || operationInFlight.current || !canRun(latestStatus.current, action)) return;
      const before = latestStatus.current;
      readRevision.current += 1;
      operationInFlight.current = true;
      setBusy(true);
      setError("");
      setNotice("");
      setConfirmStrict(false);
      let result: WifiResult | undefined;
      try {
        result = await operation();
        if (!mounted.current || !isVisible.current) return;
        if (!result.success) setError(resultMessage(result));
      } catch {
        if (mounted.current && isVisible.current) setError("The backend call ended before the result could be verified.");
      } finally {
        await inFlight.current?.catch(() => undefined);
        const verified = await refresh(true);
        if (mounted.current && isVisible.current && result?.success) {
          const policy = verified && savedPolicy(verified);
          const expected = action === "off" ? "off" : action === "prefer" ? "five_six_no_24" : action === "strict" ? "five_six_only" : null;
          const unchanged = verified && before?.live?.wifi_backend === verified.live?.wifi_backend &&
            (!before?.live?.connection_uuid || before.live.connection_uuid === verified.live?.connection_uuid);
          const frequency = Number(verified?.live?.frequency);
          const applied = expected ? policy === expected : verified?.connected && frequency >= (action === "six" ? 5925 : 4900);
          if (unchanged && applied && !verified.live?.recovery_required && !verified.live?.band_policy_error)
            setNotice(resultMessage(result));
          else setError("The final connection or setting could not be verified. Check the current WiFi state.");
        }
        operationInFlight.current = false;
        if (mounted.current) setBusy(false);
      }
    },
    [refresh],
  );

  if (!status) {
    return (
      <PanelSection>
        <PanelSectionRow>
          {statusError ? <Field focusable label="WiFi status unavailable" description={statusError} /> : <Spinner />}
        </PanelSectionRow>
        {statusError && <PanelSectionRow><ButtonItem layout="below" onClick={() => void refresh()}>Retry status</ButtonItem></PanelSectionRow>}
      </PanelSection>
    );
  }

  const settings = status.settings ?? {};
  const live = status.live ?? {};
  const policy = settings.band_policy ??
    (settings.band_preference_enabled ? "five_six_no_24" : "off");
  const preferred = policy === "five_six_no_24";
  const strict = policy === "five_six_only";
  const legacy = settings.band_policy_legacy_detected === true;
  const supported =
    settings.device_family === "legion_go_2" && settings.driver === "mt7921e";
  const frequency = live.frequency;
  const iwd = live.wifi_backend === "iwd";
  const wpa = live.wifi_backend === "wpa_supplicant";
  const inactive = wpa && (preferred || strict);
  const disabled = (action: WifiAction) => busy || !canRun(status, action);

  return (
    <>
      <PanelSection title="Current WiFi connection">
        <PanelSectionRow><Field focusable
          label={busy ? "Checking WiFi connection" : status.connected ? (live.ssid ?? "Connected WiFi") :
            live.connection_state === "connecting" ? "WiFi connecting" : live.connection_state === "unknown" ? "Connection state unavailable" : "WiFi disconnected"}
          description={busy ? "Wait for the operation and final connection check." :
            `${status.connected ? `${bandName(frequency)} · ${frequency ?? "?"} MHz${live.channel ? ` · channel ${live.channel}` : ""}` : "No verified WiFi link"} · ${iwd ? "iwd" : wpa ? "WPA supplicant" : "backend unavailable"}${live.high_band_discovery_pending ? " · searching for 5/6 GHz" : ""}`} />
        </PanelSectionRow>
        {(error || statusError || notice) && <PanelSectionRow><Field focusable
          label={error || statusError ? "Could not complete" : "Result"} description={error || statusError || notice} /></PanelSectionRow>}
        {live.band_policy_error && <PanelSectionRow><Field focusable label="Setting mismatch" description={live.band_policy_error} /></PanelSectionRow>}
      </PanelSection>
      {!supported && (
        <PanelSection title="Unsupported WiFi configuration">
          <PanelSectionRow>
            <Field
              focusable
              label={settings.device_label ?? "Unknown device"}
              description={`Driver: ${settings.driver ?? "unknown"}. This module only changes a Legion Go 2 with MT7922/mt7921e.`}
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      {live.recovery_required && (
        <PanelSection title="Recovery required">
          <PanelSectionRow>
            <Field
              focusable
              label="Network controls are locked"
              description={
                live.recovery_errors?.join("; ") ||
                "A previous transaction must be recovered before another change."
              }
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="Band preference">
        {!iwd && !wpa && <PanelSectionRow><Field focusable label="Backend unavailable"
          description="WiFi controls become available after the active backend is verified." /></PanelSectionRow>}
        {wpa && <PanelSectionRow><Field focusable label="Automatic WiFi with manual selection"
          description="SteamOS manages roaming automatically. You can reconnect once to a detected 5/6 GHz access point." /></PanelSectionRow>}
        {inactive && <PanelSectionRow><Field focusable label="Saved iwd setting inactive"
          description="The previous iwd preference is inactive with WPA. Clear the saved setting below; this does not reconnect WPA." /></PanelSectionRow>}
        {iwd && <PanelSectionRow><Field focusable label="Choose WiFi band behavior"
          description="These choices survive restart and wake. Prefer 5/6 GHz keeps 2.4 GHz as fallback. Only 5/6 GHz can leave WiFi disconnected when neither band is available." /></PanelSectionRow>}
        {policy === "six_ghz_only" && <PanelSectionRow><Field focusable label="Existing 6 GHz only policy"
          description={wpa ? "The saved WPA profile is limited to 6 GHz. Choose Automatic WiFi to restore it." :
            "The saved 6 GHz profile limit is inactive with iwd. Choose Automatic WiFi to clear the owned profile setting."} /></PanelSectionRow>}
        {legacy && <PanelSectionRow><Field focusable label="Legacy band setting detected"
          description="Restore the legacy setting first; then select a new WiFi behavior." /></PanelSectionRow>}
        {(iwd || wpa) && <PanelSectionRow><ButtonItem layout="below" disabled={disabled("off")}
          onClick={() => void run("off", () => setBandPolicy("off"))}>
          {legacy ? "Restore legacy band setting" : inactive ? "Clear saved iwd preference" : policy === "off" ? "> Automatic WiFi" : "Use Automatic WiFi"}
        </ButtonItem></PanelSectionRow>}
        {iwd && <>
        <PanelSectionRow><ButtonItem layout="below" disabled={disabled("prefer")}
          onClick={() => void run("prefer", () => setBandPolicy("five_six_no_24"))}>
          {preferred ? "> Prefer 5/6 GHz (2.4 GHz fallback)" : "Prefer 5/6 GHz (2.4 GHz fallback)"}
        </ButtonItem></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={disabled("strict")}
          onClick={() => { if (!operationInFlight.current && canRun(latestStatus.current, "strict")) setConfirmStrict(value => !value); }}>
          {strict ? "> Only 5/6 GHz" : confirmStrict ? "Cancel Only 5/6 GHz" : "Use Only 5/6 GHz"}
        </ButtonItem></PanelSectionRow>
        {confirmStrict && !strict && <>
          <PanelSectionRow><Field focusable label="Connection attempt"
            description="A scan can miss a 5/6 GHz access point. This briefly restarts WiFi and tries to connect even without a scan result. If no 5/6 GHz link is verified, Companion restores the previous setting automatically." /></PanelSectionRow>
          <PanelSectionRow><ButtonItem layout="below" disabled={disabled("strict")}
            onClick={() => void run("strict", () => setBandPolicy("five_six_only", true))}>
            Confirm Only 5/6 GHz
          </ButtonItem></PanelSectionRow>
        </>}
        {strict && !status.connected && <PanelSectionRow><Field focusable label="No 5/6 GHz connection"
          description="Only 5/6 GHz is active. Choose Automatic WiFi or Prefer 5/6 GHz to allow 2.4 GHz again." /></PanelSectionRow>}
        {strict && status.connected && bandName(frequency) === "2.4 GHz" &&
          <PanelSectionRow><Field focusable label="Unexpected 2.4 GHz connection"
            description="The live connection does not match Only 5/6 GHz. Check the result and restore WiFi settings if needed." /></PanelSectionRow>}
        </>}
        {((iwd && preferred) || (wpa && policy === "off")) && <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={disabled("high")}
            onClick={() => void run("high", rescanAndReconnect)}
          >
            {busy ? "Working..." : "Rescan and reconnect to 5/6 GHz"}
          </ButtonItem>
        </PanelSectionRow>}
        {policy === "off" && live.wifi_backend === "wpa_supplicant" && <>
          <PanelSectionRow><Field focusable label="One-time 6 GHz connection"
            description="Connects to a freshly detected 6 GHz access point, then clears the temporary selection so normal roaming remains available." /></PanelSectionRow>
          <PanelSectionRow><ButtonItem layout="below" disabled={disabled("six")}
            onClick={() => void run("six", () => rescanAndReconnect("six"))}>
            Connect to 6 GHz once
          </ButtonItem></PanelSectionRow>
        </>}
      </PanelSection>

      <PanelSection title="Safety">
        <PanelSectionRow>
          <Field
            focusable
            label="Band choice and rollback"
            description={iwd ? "Prefer 5/6 GHz keeps 2.4 GHz available. Only 5/6 GHz disables 2.4 GHz; failed changes are rolled back." : "One-time reconnect clears its temporary selection after connecting. Failed changes are rolled back automatically."}
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
