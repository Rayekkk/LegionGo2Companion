// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable } from "@decky/api";
import { ButtonItem, DropdownItem, DropdownOption, Field, PanelSection, PanelSectionRow, Spinner } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";

export type GyroSource = "system" | "combined" | "left" | "right";
type Vector = { x: number | null; y: number | null; z: number | null };
type Touchpad = { x: number | null; y: number | null; is_touching: boolean | null; raw_x?: number; raw_y?: number };

export interface ControllerStatus {
  available: boolean;
  reason: string;
  gyro_source: GyroSource;
  applied_source: Exclude<GyroSource, "system"> | null;
  controlled: boolean;
  conflict: boolean;
  error: string;
  physical: { path: string; pid: string; driver: string } | null;
  virtual: { path: string; pid: string } | null;
  iio: { name: string; path: string; frequency: number | string | null; scale: number | string | null; raw: Vector }[];
  diagnostics_active: boolean;
  recovery_pending?: boolean;
  imu?: { available: boolean; actual: { left: boolean; right: boolean } | null; reason?: string };
}

interface Stream<T> {
  received: boolean;
  reports: number;
  rate_hz: number | null;
  age_ms: number | null;
  sample: T | null;
  error?: string;
  invalid_reports?: number;
}

interface ControllerDiagnostics {
  token: string;
  active: boolean;
  reason: string;
  elapsed_s: number;
  remaining_s: number;
  physical: Stream<{
    touchpad: Touchpad | null;
    gyro_left: Vector | null;
    gyro_right: Vector | null;
    battery_left: number | null;
    battery_right: number | null;
    connection_left: string | null;
    connection_right: string | null;
  }>;
  virtual: Stream<{ gyro: Vector | null; touchpad: Touchpad | null }>;
}

export const getControllerStatus = callable<[], ControllerStatus>("controller_get_status");
const startDiagnostics = callable<[], ControllerDiagnostics>("controller_start_diagnostics");
const getDiagnostics = callable<[string], ControllerDiagnostics>("controller_get_diagnostics");
const stopDiagnostics = callable<[string], ControllerDiagnostics>("controller_stop_diagnostics");
const setGyroSource = callable<[GyroSource], ControllerStatus>("controller_set_gyro_source");
const releaseControl = callable<[], ControllerStatus>("controller_release_control");

const sourceLabels: Record<GyroSource, string> = {
  system: "System default",
  combined: "Both controllers (average)",
  left: "Left controller",
  right: "Right controller",
};
const sourceOptions: DropdownOption[] = Object.entries(sourceLabels).map(([data, label]) => ({ data, label }));
const sourceLabel = (source: GyroSource | null) => source ? sourceLabels[source] || "Unknown" : "Not confirmed";

export const controllerSummary = (status?: ControllerStatus) => {
  if (!status) return "Gyro source and controller diagnostics";
  if (!status.available) return status.reason || "Controller unavailable";
  if (status.recovery_pending) return "An interrupted gyro change needs recovery";
  if (status.conflict) return "Controller control is in use elsewhere";
  if (status.error) return "Controller setting needs attention";
  return status.controlled ? `Gyro: ${sourceLabel(status.gyro_source)}` : "Enable gyro · test gyro and touchpad";
};

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const vectorText = (value: Vector | null | undefined) => value
  ? ["x", "y", "z"].map((axis) => `${axis.toUpperCase()} ${finite(value[axis as keyof Vector]) ? value[axis as keyof Vector] : "unknown"}`).join(" · ")
  : "No reading received";
const touchText = (value: Touchpad | null | undefined) => !value || typeof value.is_touching !== "boolean"
  ? "No touchpad reading received"
  : !value.is_touching
    ? "No touch detected"
    : `Touch detected · X ${finite(value.x) ? value.x.toFixed(3) : "unknown"} · Y ${finite(value.y) ? value.y.toFixed(3) : "unknown"}`;
const streamText = (stream: Stream<unknown>) => {
  const details = !stream.received
    ? "No packets received yet. This does not establish that the sensor is faulty."
    : `${stream.reports} reports · ${finite(stream.rate_hz) ? `${stream.rate_hz.toFixed(1)} Hz` : "rate unknown"} · ${finite(stream.age_ms) ? `last packet ${(stream.age_ms / 1000).toFixed(1)} s ago` : "packet age unknown"}`;
  return stream.error ? `${stream.error} ${details}` : details;
};
const errorText = (error: unknown, fallback: string) => error instanceof Error && error.message ? error.message : fallback;
const reportingLabel = (value: unknown) => value === true ? "On" : value === false ? "Off" : "Unknown";

export const ControllerPage: FC<{ visible: boolean }> = ({ visible }) => {
  const [status, setStatus] = useState<ControllerStatus | null>(null);
  const [diagnostics, setDiagnostics] = useState<ControllerDiagnostics | null>(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const mounted = useRef(false);
  const isVisible = useRef(visible);
  const generation = useRef(0);
  const statusRevision = useRef(0);
  const statusRead = useRef<Promise<ControllerStatus> | null>(null);
  const saving = useRef(false);
  const startInFlight = useRef(false);
  const stopInFlight = useRef<Promise<unknown> | null>(null);
  const token = useRef<string | null>(null);
  const diagnosticRevision = useRef(0);
  const diagnosticTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  isVisible.current = visible;

  const stop = useCallback((reason = "Test stopped.") => {
    diagnosticRevision.current += 1;
    if (diagnosticTimer.current !== null) clearTimeout(diagnosticTimer.current);
    diagnosticTimer.current = null;
    const previousToken = token.current;
    token.current = null;
    if (mounted.current) setDiagnostics((current) => current?.active ? { ...current, active: false, reason } : current);
    if (!previousToken) return;
    const activeGeneration = generation.current;
    if (mounted.current) setStopping(true);
    const request = stopDiagnostics(previousToken);
    stopInFlight.current = request;
    void request.then((final) => {
      if (mounted.current && isVisible.current && generation.current === activeGeneration) setDiagnostics(final);
    }).catch(() => {
      if (mounted.current && isVisible.current && generation.current === activeGeneration) {
        setError("The stop request was not confirmed. The passive test expires automatically when its short lease ends.");
      }
    }).finally(() => {
      if (stopInFlight.current === request) stopInFlight.current = null;
      if (mounted.current && generation.current === activeGeneration) setStopping(false);
    });
  }, []);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
      generation.current += 1;
      statusRevision.current += 1;
      stop();
    };
  }, [stop]);

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || saving.current || statusRead.current || startInFlight.current || token.current) return;
    const revision = ++statusRevision.current;
    const request = getControllerStatus();
    statusRead.current = request;
    try {
      const next = await request;
      if (mounted.current && isVisible.current && revision === statusRevision.current) {
        setStatus(next);
        setError("");
      }
    } catch (failure) {
      if (mounted.current && isVisible.current && revision === statusRevision.current) {
        setError(errorText(failure, "Controller status is unavailable. Retrying while this page is open."));
      }
    } finally {
      if (statusRead.current === request) statusRead.current = null;
    }
  }, []);

  useEffect(() => {
    if (!visible) {
      stop("Test stopped when this page was hidden.");
      return;
    }
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => {
      clearInterval(timer);
      statusRevision.current += 1;
      stop("Test stopped when this page was hidden.");
    };
  }, [refresh, stop, visible]);

  const beginPolling = useCallback((sessionToken: string, revision: number) => {
    const valid = () => mounted.current && isVisible.current
      && token.current === sessionToken && diagnosticRevision.current === revision;
    const poll = async () => {
      diagnosticTimer.current = null;
      if (!valid()) return;
      try {
        const next = await getDiagnostics(sessionToken);
        if (!valid()) return;
        setDiagnostics(next);
        if (!next.active) {
          token.current = null;
          return;
        }
        // Schedule after completion so slow replies never overlap or queue leases.
        diagnosticTimer.current = setTimeout(() => void poll(), 350);
      } catch (failure) {
        if (!valid()) return;
        setError(errorText(failure, "The passive controller test could not continue."));
        stop("Test stopped after a diagnostic error.");
      }
    };
    diagnosticTimer.current = setTimeout(() => void poll(), 350);
  }, [stop]);

  const start = useCallback(async () => {
    if (!mounted.current || !isVisible.current || saving.current || startInFlight.current || stopInFlight.current || token.current) return;
    startInFlight.current = true;
    const revision = ++diagnosticRevision.current;
    statusRevision.current += 1;
    setStarting(true);
    setError("");
    setNotice("");
    setDiagnostics(null);
    try {
      await statusRead.current?.catch(() => undefined);
      if (!mounted.current || !isVisible.current || revision !== diagnosticRevision.current) return;
      const snapshot = await startDiagnostics();
      if (!mounted.current || !isVisible.current || revision !== diagnosticRevision.current) {
        // A delayed start can create a lease after hide/unmount; close that token too.
        await stopDiagnostics(snapshot.token).catch(() => undefined);
        return;
      }
      setDiagnostics(snapshot);
      if (snapshot.active) {
        token.current = snapshot.token;
        beginPolling(snapshot.token, revision);
      }
    } catch (failure) {
      if (mounted.current && isVisible.current && revision === diagnosticRevision.current) {
        setError(errorText(failure, "The passive controller test could not start."));
      }
    } finally {
      startInFlight.current = false;
      if (mounted.current) setStarting(false);
    }
  }, [beginPolling]);

  const apply = useCallback(async (operation: () => Promise<ControllerStatus>, message: string) => {
    // A native dropdown invokes its selection while QAM can still be hidden.
    // Visibility gates polling, not the user's explicit selection callback.
    if (!mounted.current || saving.current || startInFlight.current || stopInFlight.current || token.current) return;
    saving.current = true;
    const activeGeneration = generation.current;
    statusRevision.current += 1;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await statusRead.current?.catch(() => undefined);
      const next = await operation();
      if (!mounted.current || generation.current !== activeGeneration) return;
      setStatus(next);
      if (next.error || next.conflict) setError(next.error || next.reason || "Another controller configuration is active.");
      else setNotice(message);
    } catch (failure) {
      if (mounted.current && generation.current === activeGeneration) {
        setError(errorText(failure, "The requested gyro source could not be confirmed."));
      }
    } finally {
      saving.current = false;
      if (mounted.current && generation.current === activeGeneration) setBusy(false);
    }
  }, []);

  if (!status) return <PanelSection title="Controller">
    <PanelSectionRow>{error ? <Field label="Status unavailable" description={error} /> : <Spinner />}</PanelSectionRow>
    {error && <PanelSectionRow><ButtonItem layout="below" onClick={() => void refresh()}>Check Again</ButtonItem></PanelSectionRow>}
  </PanelSection>;

  const testing = starting || stopping || !!diagnostics?.active;
  const blocked = busy || testing;
  const actualSource = sourceLabel(status.applied_source);
  const pending = status.gyro_source !== "system" && !status.controlled;

  return <>
    <PanelSection title="Gyro source">
      <PanelSectionRow>
        <DropdownItem
          label="Controller motion source"
          description="Select Left, Right or Both to enable gyro reporting and use that source in Steam. System default restores the previous settings and releases Companion control."
          strDefaultLabel={sourceLabel(status.gyro_source)}
          selectedOption={status.gyro_source}
          rgOptions={sourceOptions}
          disabled={blocked || !status.available || status.conflict}
          onChange={(option) => {
            const source = String(option.data);
            if (!(source in sourceLabels)) return;
            void apply(() => setGyroSource(source as GyroSource), "Gyro source preference saved.");
          }}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <Field
          label={`Current motion route: ${actualSource}`}
          description={pending
            ? `Saved choice: ${sourceLabel(status.gyro_source)}. It has not yet been confirmed as applied.`
            : status.controlled
              ? "The saved gyro reporting and motion route are applied. Use the test below to check sensor readings."
              : "Companion is not maintaining a gyro-source override."}
        />
      </PanelSectionRow>
      <PanelSectionRow><Field label={`Gyro reporting: left ${reportingLabel(status.imu?.actual?.left)} · right ${reportingLabel(status.imu?.actual?.right)}`}
        description="Reporting must be enabled for controller motion to reach Steam. Both controllers uses their average; it does not use the sensor inside the console body." /></PanelSectionRow>
      {(!status.available || status.conflict || status.reason) && <PanelSectionRow>
        <Field label={status.conflict ? "Configuration conflict" : !status.available ? "Controller unavailable" : "Controller status"} description={status.reason} />
      </PanelSectionRow>}
      {status.recovery_pending && <PanelSectionRow><Field label="Recovery pending"
        description="An earlier change did not finish. The previous controller settings are retained for recovery. Release Companion Control to restore them." /></PanelSectionRow>}
      {(status.controlled || status.gyro_source !== "system" || status.conflict || status.recovery_pending) && <PanelSectionRow>
        <ButtonItem layout="below" disabled={blocked} onClick={() => void apply(releaseControl, "Companion gyro control released.")}>
          Release Companion Control
        </ButtonItem>
      </PanelSectionRow>}
    </PanelSection>

    <PanelSection title="Passive controller test">
      <PanelSectionRow>
        <Field
          label="Compare physical and Steam readings"
          description="Move each controller and touch the touchpad during a 30-second test. It observes existing reports without enabling sensors or changing controller mode. It stops when this page is hidden."
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy || starting || stopping || (!diagnostics?.active && !status.physical)}
          onClick={() => diagnostics?.active ? stop() : void start()}
        >
          {starting ? "Starting Test…" : stopping ? "Stopping Test…" : diagnostics?.active ? "Stop Test" : "Start 30-Second Test"}
        </ButtonItem>
      </PanelSectionRow>
      {diagnostics && <PanelSectionRow>
        <Field
          label={diagnostics.active ? `Test running · ${finite(diagnostics.remaining_s) ? Math.ceil(diagnostics.remaining_s) : "unknown"} s remaining` : "Test finished"}
          description={diagnostics.reason || "Raw gyro values are sensor counts, not degrees per second. Physical and virtual readings may use different scales."}
        />
      </PanelSectionRow>}
    </PanelSection>

    {diagnostics && <>
      <PanelSection title="Physical controllers">
        <PanelSectionRow><Field label="Controller reports" description={streamText(diagnostics.physical)} /></PanelSectionRow>
        <PanelSectionRow><Field label="Left gyro · raw counts" description={vectorText(diagnostics.physical.sample?.gyro_left)} /></PanelSectionRow>
        <PanelSectionRow><Field label="Right gyro · raw counts" description={vectorText(diagnostics.physical.sample?.gyro_right)} /></PanelSectionRow>
        <PanelSectionRow><Field label="Physical touchpad" description={touchText(diagnostics.physical.sample?.touchpad)} /></PanelSectionRow>
      </PanelSection>
      <PanelSection title="Existing Steam controller">
        <PanelSectionRow><Field label="Virtual controller reports" description={streamText(diagnostics.virtual)} /></PanelSectionRow>
        <PanelSectionRow><Field label="Steam gyro · raw counts" description={vectorText(diagnostics.virtual.sample?.gyro)} /></PanelSectionRow>
        <PanelSectionRow><Field label="Steam touchpad" description={touchText(diagnostics.virtual.sample?.touchpad)} /></PanelSectionRow>
      </PanelSection>
    </>}

    {(error || status.error || notice || busy) && <PanelSection title={busy ? "Applying" : error || status.error ? "Needs attention" : "Saved"}>
      <PanelSectionRow>
        <Field label={busy ? "Updating the gyro source" : error || status.error ? "Controller status" : "Preference confirmed"} description={busy ? "Waiting for the controller to confirm the change." : error || status.error || notice} />
      </PanelSectionRow>
    </PanelSection>}
  </>;
};
