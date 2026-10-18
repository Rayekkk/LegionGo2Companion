// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable, toaster, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { detailsRows, infoRow, mutationQueue, statusPlaceholder } from "./shared";

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
  recovery_pending?: boolean;
}

export interface ControllerBatteryStatus {
  available: boolean;
  left: number | null;
  right: number | null;
  connection_left: "connecting" | "attached" | "detached" | null;
  connection_right: "connecting" | "attached" | "detached" | null;
  reason?: string;
}

interface BatteryResult {
  success: boolean;
  error?: string;
  status?: BatteryStatus;
}

const mutations = mutationQueue();
const readBatteryStatus = callable<[], BatteryStatus>("battery_get_status");
export const getBatteryStatus = async () => {
  if (mutations.pending) await mutations.tail;
  return readBatteryStatus();
};
export const getControllerBatteryLevels = callable<[], ControllerBatteryStatus>("battery_get_controller_levels");
const setBatteryEnabled = callable<[boolean], BatteryResult>("battery_set_enabled");
const releaseBatteryControl = callable<[], BatteryResult>("battery_release_control");

const unavailableControllerBatteries = (): ControllerBatteryStatus => ({
  available: false,
  left: null,
  right: null,
  connection_left: null,
  connection_right: null,
  reason: "Controller battery levels are unavailable.",
});

const protectionLabel = (enabled: boolean | null) =>
  enabled === true ? "On" : enabled === false ? "Off" : "Unknown";
const notify = (body: string) => toaster.toast({ title: "Battery", body });

const validControllerLevel = (value: number | null | undefined): value is number =>
  typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;

const controllerLevel = (value: number | null | undefined, connection: string | null | undefined) =>
  validControllerLevel(value)
    ? `${Math.round(value)}%`
    : connection === "detached" ? "Not connected" : "Unknown";

/** One controller in the battery line: "left 80%", "left 80% (detached)" or "left —". */
const controllerShort = (side: string, value: number | null | undefined, connection: string | null | undefined) =>
  `${side} ${validControllerLevel(value) ? `${Math.round(value)}%` : "—"}${connection === "detached" && validControllerLevel(value) ? " (detached)" : ""}`;

export const batterySummary = (status?: BatteryStatus, controllers?: ControllerBatteryStatus) => {
  const levels = controllers ? ` · L ${controllerLevel(controllers.left, controllers.connection_left)}`
    + ` · R ${controllerLevel(controllers.right, controllers.connection_right)}` : "";
  if (!status) return `Battery protection and charging state${levels}`;
  if (status.recovery_pending) return `An interrupted battery change needs recovery${levels}`;
  if (!status.supported) return `${status.reason || "Battery protection unavailable"}${levels}`;
  if (typeof status.enabled !== "boolean") return `Battery protection status unavailable${levels}`;
  const pending = status.managed
    && typeof status.requested_enabled === "boolean"
    && status.requested_enabled !== status.enabled;
  return `Protection ${status.enabled ? "on" : "off"}${pending ? " · saved choice pending" : ""}${levels}`;
};

export const BatteryPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const [status, setStatus] = useState<BatteryStatus | null>(null);
  const [controllers, setControllers] = useState<ControllerBatteryStatus>(unavailableControllerBatteries);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The last change's failure stays on its control until the next change.
  const [failed, setFailed] = useState<{ control: "toggle" | "restore"; text: string } | null>(null);
  const [details, setDetails] = useState(false);
  const mounted = useRef(false);
  const isVisible = useRef(visible);
  const saving = useRef(false);
  const generation = useRef(0);
  const readRevision = useRef(0);
  const readInFlight = useRef<Promise<BatteryStatus | undefined> | null>(null);
  const controllerReadInFlight = useRef<Promise<ControllerBatteryStatus> | null>(null);
  const controllerReadRevision = useRef(0);
  const refreshAfterRead = useRef(false);
  const refreshControllersAfterRead = useRef(false);
  isVisible.current = visible;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
      readRevision.current += 1;
      controllerReadRevision.current += 1;
    };
  }, []);

  const refreshConsole = useCallback(async () => {
    if (!mounted.current || !isVisible.current || saving.current || readInFlight.current) return;
    const revision = ++readRevision.current;
    const request = (async () => {
      if (mutations.pending) await mutations.tail;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      return getBatteryStatus();
    })();
    readInFlight.current = request;
    try {
      const next = await request;
      if (!next || !mounted.current || !isVisible.current || revision !== readRevision.current) return;
      setStatus(next);
      setError(next.error || (!next.success ? "Battery status could not be read." : ""));
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        setError("Battery status is unavailable. Retrying while this page is open.");
      }
    } finally {
      if (readInFlight.current === request) readInFlight.current = null;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refreshConsole(); }
    }
  }, []);

  const refreshControllers = useCallback(async () => {
    if (!mounted.current || !isVisible.current || controllerReadInFlight.current) return;
    const revision = ++controllerReadRevision.current;
    const request = getControllerBatteryLevels().catch(unavailableControllerBatteries);
    controllerReadInFlight.current = request;
    try {
      const next = await request;
      if (mounted.current && isVisible.current && revision === controllerReadRevision.current) setControllers(next);
    } finally {
      if (controllerReadInFlight.current === request) controllerReadInFlight.current = null;
      if (refreshControllersAfterRead.current) { refreshControllersAfterRead.current = false; void refreshControllers(); }
    }
  }, []);

  const refresh = useCallback(() => {
    void refreshConsole();
    void refreshControllers();
  }, [refreshConsole, refreshControllers]);

  useEffect(() => {
    if (!visible) return;
    if (readInFlight.current) refreshAfterRead.current = true;
    if (controllerReadInFlight.current) refreshControllersAfterRead.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => {
      clearInterval(timer);
      readRevision.current += 1;
      controllerReadRevision.current += 1;
    };
  }, [refresh, visible]);

  const apply = useCallback(async (operation: () => Promise<BatteryResult>, message: string, control: "toggle" | "restore") => {
    if (!mounted.current || !isVisible.current || saving.current) return;
    saving.current = true;
    const previousRead = readInFlight.current;
    readRevision.current += 1;
    const activeGeneration = generation.current;
    const canUpdate = () => mounted.current && activeGeneration === generation.current;
    setBusy(true);
    setFailed(null);
    const fail = (text: string) => { setFailed({ control, text }); notify(text); };
    const run = async () => {
      try {
        // Finish the earlier read before writing, and ignore its stale result.
        await previousRead?.catch(() => undefined);
        const result = await operation();
        // Verification belongs to this transaction; the public menu getter
        // waits for its tail and must not be called from inside that same tail.
        const next = result.status ?? (result.success ? await readBatteryStatus() : undefined);
        if (!canUpdate()) return;
        if (next) setStatus(next);
        if (!result.success) fail(result.error || next?.error || "The charging change was not confirmed.");
        else if (next?.error || next?.success === false) {
          fail(next.error || "The choice was saved, but the battery state could not be confirmed.");
        } else notify(message);
      } catch {
        if (canUpdate()) fail("The change could not be confirmed. Check the battery state before trying again.");
      } finally {
        mutations.done();
        saving.current = false;
        if (canUpdate()) setBusy(false);
      }
    };
    await mutations.push(run);
  }, []);

  if (!status) return statusPlaceholder({ title: "Battery", label: "Status unavailable", error,
    onRetry: () => void refresh(), retryLabel: "Check again" });

  const actualKnown = typeof status.enabled === "boolean";
  const savedKnown = status.managed && typeof status.requested_enabled === "boolean";
  const mismatch = savedKnown && status.requested_enabled !== status.enabled;
  const blocked = busy || !status.supported || !actualKnown || status.recovery_pending === true;
  const capacity = typeof status.capacity === "number"
    && Number.isFinite(status.capacity)
    && status.capacity >= 0
    && status.capacity <= 100
    ? `${Math.round(status.capacity)}%`
    : "—";
  const restore = () => void apply(() => releaseBatteryControl(), "Previous charging mode restored.", "restore");
  const restoreButton = <PanelSectionRow>
    <ButtonItem layout="below" disabled={busy} description={failed?.control === "restore" ? failed.text : undefined}
      onClick={restore}>Restore previous charging mode</ButtonItem>
  </PanelSectionRow>;

  return <PanelSection title="Battery">
    {status.recovery_pending ? <>
      {infoRow("A charging change didn't finish", "Restore the previous charging mode before changing it again.")}
      {restoreButton}
    </> : !status.supported ? infoRow("Battery protection unavailable",
      status.reason || "A compatible battery charging interface was not detected.")
      : error && infoRow("Battery status not updated", error)}
    {infoRow(`Battery ${capacity}${status.charging_status ? ` · ${status.charging_status}` : ""}`,
      `Controllers: ${controllerShort("left", controllers.left, controllers.connection_left)} · ${
        controllerShort("right", controllers.right, controllers.connection_right)}`)}
    {status.supported && <PanelSectionRow>
      <ToggleField
        label="Battery protection"
        description={busy ? "Applying…" : failed?.control === "toggle" ? failed.text
          : `Stops charging at about 80% to protect the battery.${!actualKnown ? " Current state unknown."
            : mismatch ? " Waiting for the battery to confirm…" : ""}`}
        checked={savedKnown ? status.requested_enabled === true : status.enabled === true}
        disabled={blocked}
        onChange={(enabled) => void apply(
          () => setBatteryEnabled(enabled),
          enabled ? "Battery protection on." : "Battery protection off.",
          "toggle",
        )}
      />
    </PanelSectionRow>}
    {detailsRows(details, () => setDetails(value => !value), [
      ["Charging mode reported", status.current_mode || "Unknown"],
      ["Saved choice", status.managed ? protectionLabel(status.requested_enabled) : "None · Companion is not managing charging"],
      ...(status.reason ? [["Battery status", status.reason] as [string, string]] : []),
      ...(!controllers.available && controllers.reason ? [["Controllers", controllers.reason] as [string, string]] : []),
    ], status.managed && !status.recovery_pending ? restoreButton : undefined)}
  </PanelSection>;
};
