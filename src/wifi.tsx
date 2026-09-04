// SPDX-License-Identifier: BSD-3-Clause

import { callable, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  Spinner,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";

interface WifiSettings {
  device_family?: string;
  device_label?: string;
  driver?: string;
  chip_label?: string;
  band_policy?: "off" | "five_six_no_24" | "six_ghz_only";
  band_preference_enabled?: boolean;
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
const setBandPreference = callable<[enabled: boolean], WifiResult>(
  "wifi_set_band_preference",
);
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
  const enabled = status.settings.band_preference_enabled === true;
  if (!status.connected) return enabled ? "Preference on · disconnected" : "Preference off";
  return `${enabled ? "Preference on" : "Preference off"} · ${bandName(
    status.live?.frequency,
  )}`;
};

const resultMessage = (result: WifiResult) =>
  [result.message, result.detail].filter(Boolean).join(" — ") ||
  "The operation could not be verified.";

export const WifiPage: FC = () => {
  const visible = useQuickAccessVisible();
  const inFlight = useRef(false);
  const operationInFlight = useRef(false);
  const [status, setStatus] = useState<WifiStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");

  const refresh = useCallback(async () => {
    if (inFlight.current || operationInFlight.current) return;
    inFlight.current = true;
    try {
      const next = await getWifiStatus();
      setStatus(next);
      if (!next.success) setError(next.message ?? "WiFi status is unavailable.");
    } catch {
      setError("WiFi status is unavailable. Retrying while this page is open.");
    } finally {
      inFlight.current = false;
    }
  }, []);

  useEffect(() => {
    void refresh();
    if (!visible) return;
    const timer = setInterval(() => void refresh(), 10000);
    return () => clearInterval(timer);
  }, [refresh, visible]);

  const run = useCallback(
    async (operation: () => Promise<WifiResult>) => {
      if (operationInFlight.current) return;
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
        operationInFlight.current = false;
        await refresh();
        setBusy(false);
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
  const enabled = settings.band_preference_enabled === true;
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
        <PanelSectionRow>
          <ToggleField
            label="Prefer 5/6 GHz"
            description="Strongly prefers 5 or 6 GHz while retaining 2.4 GHz as a fallback. The setting is system-wide and survives restart and wake."
            checked={enabled}
            disabled={blocked || !supported}
            onChange={(value) => void run(() => setBandPreference(value))}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={blocked || !supported || !enabled || !status.connected}
            onClick={() => void run(rescanAndReconnect)}
          >
            {busy ? "Working..." : "Rescan and reconnect to 5/6 GHz"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <Field
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
            <Field label="Setting mismatch" description={live.band_policy_error} />
          </PanelSectionRow>
        )}
        {(error || notice) && (
          <PanelSectionRow>
            <Field
              label={error ? "Could not complete" : "Result"}
              description={error || notice}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Safety">
        <PanelSectionRow>
          <Field
            label="No permanent band or BSSID lock"
            description="After a fresh scan, manual reconnect temporarily selects one confirmed 5/6 GHz access point for a single connection, then clears that selection. Automatic rollback restores the profile if anything fails; 2.4 GHz remains available."
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
