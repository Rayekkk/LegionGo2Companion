// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

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

export interface BatteryStatus {
  success: boolean;
  supported: boolean;
  managed: boolean;
  enabled: boolean | null;
  requested_enabled: boolean | null;
  charging_status?: string | null;
  capacity?: number | null;
  reason?: string;
  error?: string;
  backend: "charge_types";
  options?: string[];
  current_mode?: string | null;
  baseline?: string | null;
  recovery_pending?: boolean;
}

interface BatteryResult {
  success: boolean;
  error?: string;
  status?: BatteryStatus;
}

export const getBatteryStatus = callable<[], BatteryStatus>("battery_get_status");
const setBatteryEnabled = callable<[boolean], BatteryResult>("battery_set_enabled");
const releaseBatteryControl = callable<[], BatteryResult>("battery_release_control");

const protectionLabel = (enabled: boolean | null) =>
  enabled === true ? "On" : enabled === false ? "Off" : "Unknown";

export const batterySummary = (status?: BatteryStatus) => {
  if (!status) return "Battery protection and charging state";
  if (status.recovery_pending) return "An interrupted battery change needs recovery";
  if (!status.supported) return status.reason || "Battery protection unavailable";
  if (typeof status.enabled !== "boolean") return "Battery protection status unavailable";
  const pending = status.managed
    && typeof status.requested_enabled === "boolean"
    && status.requested_enabled !== status.enabled;
  return `Protection ${status.enabled ? "on · about 80% limit" : "off"}${pending ? " · saved choice pending" : ""}`;
};

export const BatteryPage: FC = () => {
  const visible = useQuickAccessVisible();
  const [status, setStatus] = useState<BatteryStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const mounted = useRef(false);
  const isVisible = useRef(visible);
  const saving = useRef(false);
  const generation = useRef(0);
  const readRevision = useRef(0);
  const readInFlight = useRef<Promise<BatteryStatus> | null>(null);
  isVisible.current = visible;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
      readRevision.current += 1;
    };
  }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || saving.current || readInFlight.current) return;
    const revision = ++readRevision.current;
    const request = getBatteryStatus();
    readInFlight.current = request;
    try {
      const next = await request;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      setStatus(next);
      setError(next.error || (!next.success ? "Battery status could not be read." : ""));
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        setError("Battery status is unavailable. Retrying while this page is open.");
      }
    } finally {
      if (readInFlight.current === request) readInFlight.current = null;
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => {
      clearInterval(timer);
      readRevision.current += 1;
    };
  }, [refresh, visible]);

  const apply = useCallback(async (operation: () => Promise<BatteryResult>, message: string) => {
    if (!mounted.current || !isVisible.current || saving.current) return;
    saving.current = true;
    readRevision.current += 1;
    const activeGeneration = generation.current;
    const canUpdate = () => mounted.current && activeGeneration === generation.current;
    setBusy(true);
    setNotice("");
    setError("");
    try {
      // Finish the earlier read before writing, and ignore its stale result.
      await readInFlight.current?.catch(() => undefined);
      const result = await operation();
      const next = result.status ?? (result.success ? await getBatteryStatus() : undefined);
      if (!canUpdate()) return;
      if (next) setStatus(next);
      if (!result.success) setError(result.error || next?.error || "The requested charging change was not confirmed.");
      else if (next?.error || next?.success === false) {
        setError(next.error || "The preference was saved, but the current battery state could not be confirmed.");
      } else setNotice(message);
    } catch {
      if (canUpdate()) {
        setError("The battery operation could not be confirmed. Check the current and saved states before trying again.");
      }
    } finally {
      saving.current = false;
      if (canUpdate()) setBusy(false);
    }
  }, []);

  if (!status) return <PanelSection title="Battery protection">
    <PanelSectionRow>
      {error ? <Field label="Status unavailable" description={error} /> : <Spinner />}
    </PanelSectionRow>
    {error && <PanelSectionRow>
      <ButtonItem layout="below" onClick={() => void refresh()}>Check Again</ButtonItem>
    </PanelSectionRow>}
  </PanelSection>;

  const actualKnown = typeof status.enabled === "boolean";
  const savedKnown = status.managed && typeof status.requested_enabled === "boolean";
  const mismatch = savedKnown && status.requested_enabled !== status.enabled;
  const blocked = busy || !status.supported || !actualKnown || status.recovery_pending === true;
  const capacity = typeof status.capacity === "number"
    && Number.isFinite(status.capacity)
    && status.capacity >= 0
    && status.capacity <= 100
    ? `${Math.round(status.capacity)}%`
    : "Unknown";

  return <>
    <PanelSection title="Battery protection">
      {status.recovery_pending && <PanelSectionRow>
        <Field
          label="Recovery pending"
          description="A previous charging change did not finish. The original setting has been kept for recovery. Recover and release control before making another change."
        />
      </PanelSectionRow>}
      {status.supported && (actualKnown || savedKnown) && <PanelSectionRow>
        <ToggleField
          label="Battery protection"
          description="Use Lenovo's protection mode to limit charging to about 80%. Turning this off restores the previous charging mode."
          checked={savedKnown ? status.requested_enabled === true : status.enabled === true}
          disabled={blocked}
          onChange={(enabled) => void apply(
            () => setBatteryEnabled(enabled),
            "Battery protection preference saved.",
          )}
        />
      </PanelSectionRow>}
      {!status.supported && <PanelSectionRow>
        <Field
          label="Battery protection unavailable"
          description={status.reason || "A compatible battery charging interface was not detected."}
        />
      </PanelSectionRow>}
      <PanelSectionRow>
        <Field
          label={`Current protection: ${protectionLabel(status.enabled)}`}
          description={actualKnown
            ? status.enabled
              ? "The battery currently reports protection mode. If it is already above the limit, enabling protection does not actively discharge it."
              : "The battery currently reports its normal charging mode."
            : "The current protection state could not be confirmed. No on/off state is assumed."}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <Field
          label={status.managed
            ? `Saved preference: ${protectionLabel(status.requested_enabled)}`
            : "Companion control: released"}
          description={status.managed
            ? mismatch
              ? "The saved choice does not match the current battery state. Companion has not yet confirmed that it is applied."
              : savedKnown && actualKnown
                ? "The saved choice matches the current battery state."
                : "The saved choice or current battery state is unavailable."
            : "Companion is not maintaining a charging preference. The toggle can enable Companion control."}
        />
      </PanelSectionRow>
      {(status.managed || status.recovery_pending) && <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy}
          onClick={() => void apply(
            () => releaseBatteryControl(),
            status.recovery_pending
              ? "Interrupted change recovered and Companion control released."
              : "Companion control released.",
          )}
        >
          {status.recovery_pending ? "Recover and Release Control" : "Release Companion Control"}
        </ButtonItem>
      </PanelSectionRow>}
    </PanelSection>

    <PanelSection title="Charging state">
      <PanelSectionRow>
        <Field label={`Battery: ${capacity}`} description={status.charging_status || "Charging status unavailable"} />
      </PanelSectionRow>
      {status.supported && status.reason && <PanelSectionRow>
        <Field label="Battery status" description={status.reason} />
      </PanelSectionRow>}
    </PanelSection>

    {(notice || error || busy) && <PanelSection title={busy ? "Applying" : error ? "Could not confirm" : "Saved"}>
      <PanelSectionRow>
        <Field
          label={busy ? "Updating battery protection" : error ? "Check the battery state" : "Preference confirmed"}
          description={busy ? "Waiting for the battery to confirm the change." : error || notice}
        />
      </PanelSectionRow>
    </PanelSection>}
  </>;
};
