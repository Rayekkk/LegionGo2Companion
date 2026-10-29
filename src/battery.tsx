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
import type { Result } from "./result";
import { detailsRows, infoRow, mutationQueue, statusPlaceholder, rpc } from "./shared";
import { controllersText, levelText, protectionChecked, protectionLocked, protectionNote, RESET_DONE, RESET_LABEL, RESET_NOTE, resetShown } from "../desktop/native/battery.mjs";

export interface BatteryStatus {
  supported: boolean;
  managed: boolean;
  enabled: boolean | null;
  requested_enabled: boolean | null;
  charging_status?: string | null;
  capacity?: number | null;
  reason?: string;
  /** Set exactly when the status is not confirmed (the read answers a refusal that carries it). */
  error?: string;
  backend: "charge_types";
  options?: string[];
  current_mode?: string | null;
  recovery_pending?: boolean;
  mode_conflict?: boolean;
}

export interface ControllerBatteryStatus {
  available: boolean;
  left: number | null;
  right: number | null;
  connection_left: "connecting" | "attached" | "detached" | null;
  connection_right: "connecting" | "attached" | "detached" | null;
  reason?: string;
}

// A change answers D2 itself: the new status, also with a refusal (a refusal before any change has none).
type BatteryResult = Result<BatteryStatus>;
const UNCONFIRMED = "The change could not be confirmed. Check the battery state before trying again.";

const mutations = mutationQueue();
const readBatteryStatus = rpc<[], BatteryStatus>("battery_get_status");
export const getBatteryStatus = async () => {
  if (mutations.pending) await mutations.tail;
  return readBatteryStatus();
};
export const getControllerBatteryLevels = rpc<[], ControllerBatteryStatus>("battery_get_controller_levels");
const setBatteryEnabled = callable<[boolean], BatteryResult>("battery_set_enabled");
const releaseBatteryControl = callable<[], BatteryResult>("battery_release_control");
const resetChargingMode = callable<[], BatteryResult>("battery_reset_charging_mode");

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

const controllerLevel = (value: number | null | undefined, connection: string | null | undefined) =>
  levelText(value) !== "—" ? levelText(value) : connection === "detached" ? "Not connected" : "Unknown";

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

/** controllers: the console's controllers report their batteries (the Gyro & touchpad module applies). */
export const BatteryPage: FC<{ visible?: boolean; controllers?: boolean }> = ({ visible: tabShown = true, controllers: controllersShown = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const [status, setStatus] = useState<BatteryStatus | null>(null);
  const [controllers, setControllers] = useState<ControllerBatteryStatus>(unavailableControllerBatteries);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The last change's failure stays on its control until the next change.
  const [failed, setFailed] = useState<{ control: "toggle" | "restore" | "reset"; text: string } | null>(null);
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
      setError(next.error || "");
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
    if (controllersShown) void refreshControllers();
  }, [refreshConsole, refreshControllers, controllersShown]);

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

  const apply = useCallback(async (operation: () => Promise<BatteryResult>, message: string, control: "toggle" | "restore" | "reset") => {
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
        const { ok, error, state } = await operation();
        // Verification belongs to this transaction; the public menu getter
        // waits for its tail and must not be called from inside that same tail.
        const next = state ?? (ok ? await readBatteryStatus() : undefined);
        if (!canUpdate()) return;
        if (next) setStatus(next);
        if (!ok) fail(next ? error || next.error || "The charging change was not confirmed." : UNCONFIRMED);
        else if (next?.error) fail(next.error);
        else notify(message);
      } catch {
        if (canUpdate()) fail(UNCONFIRMED);
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

  const capacity = levelText(status.capacity);
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
    {resetShown(status) && <PanelSectionRow>
      <ButtonItem layout="below" disabled={busy} description={failed?.control === "reset" ? failed.text : RESET_NOTE}
        onClick={() => void apply(() => resetChargingMode(), RESET_DONE, "reset")}>{RESET_LABEL}</ButtonItem>
    </PanelSectionRow>}
    {infoRow(`Battery ${capacity}${status.charging_status ? ` · ${status.charging_status}` : ""}`,
      controllersShown ? `Controllers: ${controllersText(controllers)}` : undefined)}
    {status.supported && <PanelSectionRow>
      <ToggleField
        label="Battery protection"
        description={busy ? "Applying…" : failed?.control === "toggle" ? failed.text : protectionNote(status)}
        checked={protectionChecked(status)}
        disabled={busy || protectionLocked(status)}
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
      ...(controllersShown && !controllers.available && controllers.reason ? [["Controllers", controllers.reason] as [string, string]] : []),
    ], status.managed && !status.recovery_pending ? restoreButton : undefined)}
  </PanelSection>;
};
