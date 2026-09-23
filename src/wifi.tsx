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
  signal_dbm?: string;
  tx_bitrate?: string;
  frequency?: string;
  channel?: string;
  wifi_backend?: string;
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
const rescanAndReconnect = callable<[], WifiResult>(
  "wifi_rescan_and_reconnect",
);
const resetWifiSettings = callable<[], WifiResult>("wifi_reset_settings");

const bandName = (frequency?: string | number | null) => {
  const value = Number(frequency);
  if (!Number.isFinite(value) || value <= 0) return "Unknown band";
  if (value < 3000) return "2.4 GHz";
  if (value < 5925) return "5 GHz";
  return "6 GHz";
};

export const wifiSummary = (status?: WifiStatus) => {
  if (!status?.success || !status.settings) return "5/6 GHz preference";
  const policy = status.settings.band_policy ??
    (status.settings.band_preference_enabled ? "five_six_no_24" : "off");
  const label = status.settings.band_policy_legacy_detected ? "Legacy 5 GHz setting"
    : policy === "six_ghz_only" ? "6 GHz only"
    : policy === "five_six_only" ? "5/6 GHz only"
    : policy === "five_six_no_24" ? "5/6 GHz preferred" : "Automatic WiFi";
  return `${label} · ${status.connected ? bandName(status.live?.frequency) : "disconnected"}`;
};

const resultMessage = (result: WifiResult) =>
  [result.message, result.detail].filter(Boolean).join(" — ") ||
  "The operation could not be verified.";

export const WifiPage: FC = () => {
  const visible = useQuickAccessVisible();
  const inFlight = useRef<Promise<WifiStatus> | null>(null);
  const operationInFlight = useRef(false);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
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

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || inFlight.current || operationInFlight.current) return;
    const revision = readRevision.current;
    const request = getWifiStatus();
    inFlight.current = request;
    try {
      const next = await request;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      setStatus(next);
      setStatusError(next.success ? "" : next.message ?? "WiFi status is unavailable.");
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current)
        setStatusError("WiFi status is unavailable. Retrying while this page is open.");
    } finally {
      if (inFlight.current === request) inFlight.current = null;
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); readRevision.current += 1; };
  }, [refresh, visible]);

  const run = useCallback(
    async (operation: () => Promise<WifiResult>) => {
      if (operationInFlight.current) return;
      readRevision.current += 1;
      operationInFlight.current = true;
      setBusy(true);
      setError("");
      setNotice("");
      try {
        const result = await operation();
        if (result.success) setNotice(resultMessage(result));
        else setError(resultMessage(result));
        if (result.reconnected) {
          await new Promise((resolve) => setTimeout(resolve, 3000));
        }
      } catch {
        setError("The backend call ended before the result could be verified.");
      } finally {
        await inFlight.current?.catch(() => undefined);
        operationInFlight.current = false;
        await refresh();
        if (mounted.current) setBusy(false);
      }
    },
    [refresh],
  );

  if (!status) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Spinner />
        </PanelSectionRow>
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
  const blocked = busy || live.recovery_required === true;
  const frequency = live.frequency;

  return (
    <>
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
        <PanelSectionRow><Field focusable label="Choose WiFi band behavior"
          description="These choices apply system-wide and survive restart and wake. Only 5/6 GHz can leave WiFi disconnected when neither band is available." /></PanelSectionRow>
        {policy === "six_ghz_only" && <PanelSectionRow><Field focusable label="Existing 6 GHz only policy"
          description="The saved profile is limited to 6 GHz. Choose another behavior below to replace this policy." /></PanelSectionRow>}
        {legacy && <PanelSectionRow><Field focusable label="Legacy band setting detected"
          description="Restore the legacy setting first; then select a new WiFi behavior." /></PanelSectionRow>}
        <PanelSectionRow><ButtonItem layout="below" disabled={blocked || !supported || (policy === "off" && !legacy)}
          onClick={() => { setConfirmStrict(false); void run(() => setBandPolicy("off")); }}>
          {legacy ? "Restore legacy band setting" : policy === "off" ? "> Automatic WiFi" : "Use Automatic WiFi"}
        </ButtonItem></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={blocked || !supported || legacy || preferred}
          onClick={() => { setConfirmStrict(false); void run(() => setBandPolicy("five_six_no_24")); }}>
          {preferred ? "> Prefer 5/6 GHz (2.4 GHz fallback)" : "Prefer 5/6 GHz (2.4 GHz fallback)"}
        </ButtonItem></PanelSectionRow>
        <PanelSectionRow><ButtonItem layout="below" disabled={blocked || !supported || legacy || strict}
          onClick={() => setConfirmStrict(value => !value)}>
          {strict ? "> Only 5/6 GHz" : confirmStrict ? "Cancel Only 5/6 GHz" : "Use Only 5/6 GHz"}
        </ButtonItem></PanelSectionRow>
        {confirmStrict && !strict && <>
          <PanelSectionRow><Field focusable label="Connection attempt"
            description="A scan can miss a 5/6 GHz access point. This briefly restarts WiFi and tries to connect even without a scan result. If no 5/6 GHz link is verified, Companion restores the previous setting automatically." /></PanelSectionRow>
          <PanelSectionRow><ButtonItem layout="below" disabled={blocked || !supported || legacy}
            onClick={() => { setConfirmStrict(false); void run(() => setBandPolicy("five_six_only", true)); }}>
            Confirm Only 5/6 GHz
          </ButtonItem></PanelSectionRow>
        </>}
        {strict && !status.connected && <PanelSectionRow><Field focusable label="No 5/6 GHz connection"
          description="Only 5/6 GHz is active. Choose Automatic WiFi or Prefer 5/6 GHz to allow 2.4 GHz again." /></PanelSectionRow>}
        {strict && status.connected && bandName(frequency) === "2.4 GHz" &&
          <PanelSectionRow><Field focusable label="Unexpected 2.4 GHz connection"
            description="The live connection does not match Only 5/6 GHz. Check the result and restore WiFi settings if needed." /></PanelSectionRow>}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={blocked || !supported || !preferred || !status.connected}
            onClick={() => void run(rescanAndReconnect)}
          >
            {busy ? "Working..." : "Rescan and reconnect to 5/6 GHz"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
            focusable
            label={status.connected ? bandName(frequency) : "WiFi disconnected"}
            description={
              status.connected
                ? `${frequency ?? "?"} MHz${live.channel ? ` · channel ${live.channel}` : ""}${live.signal_dbm ? ` · ${live.signal_dbm}` : ""}`
                : "Connect to WiFi before using manual rescan."
            }
          />
        </PanelSectionRow>
        {live.band_policy_error && (
          <PanelSectionRow>
            <Field focusable label="Setting mismatch" description={live.band_policy_error} />
          </PanelSectionRow>
        )}
        {(error || statusError || notice) && (
          <PanelSectionRow>
            <Field
              focusable
              label={error || statusError ? "Could not complete" : "Result"}
              description={error || statusError || notice}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Safety">
        <PanelSectionRow>
          <Field
            focusable
            label="Band choice and rollback"
            description="The preference keeps 2.4 GHz available. Manual reconnect temporarily selects one confirmed 5/6 GHz access point and clears that selection after connecting. Only 5/6 GHz disables 2.4 GHz in iwd; a failed change is rolled back."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={blocked}
            onClick={() => void run(resetWifiSettings)}
          >
            Restore WiFi settings owned by Companion
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
