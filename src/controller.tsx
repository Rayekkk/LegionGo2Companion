// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { toaster } from "@decky/api";
import { ButtonItem, DropdownItem, DropdownOption, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { FirmwarePanel } from "./firmware";
import { detailsRows, errorText, infoRow, mutationQueue, statusPlaceholder, rpc } from "./shared";
import { reportingText, SLEEP_DEFAULT, SLEEP_LABEL, SLEEP_TITLE, sleepChange, sleepNote, sleepOptions, sleepShown, sleepText,
  sleepValue, SOURCE_LABELS as sourceLabels, sourceLabel, sourceLocked, sourceNote, streamText, touchText,
  vectorText } from "../desktop/native/controller.mjs";

type GyroSource = "system" | "combined" | "left" | "right";
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
  recovery_pending?: boolean;
  imu?: { available: boolean; actual: { left: boolean; right: boolean } | null; reason?: string };
  sleep?: { available: boolean; minutes: number | null; error: string };
}

interface Stream<T> {
  received: boolean;
  reports: number;
  rate_hz: number | null;
  sample: T | null;
  error?: string;
}

interface ControllerDiagnostics {
  token: string;
  active: boolean;
  reason: string;
  remaining_s: number;
  physical: Stream<{
    touchpad: Touchpad | null;
    gyro_left: Vector | null;
    gyro_right: Vector | null;
    connection_left: string | null;
    connection_right: string | null;
  }>;
  virtual: Stream<{ gyro: Vector | null; touchpad: Touchpad | null }>;
}

const mutations = mutationQueue();
const readControllerStatus = rpc<[], ControllerStatus>("controller_get_status");
export const getControllerStatus = async () => {
  if (mutations.pending) await mutations.tail;
  return readControllerStatus();
};
const startDiagnostics = rpc<[], ControllerDiagnostics>("controller_start_diagnostics");
const getDiagnostics = rpc<[string], ControllerDiagnostics>("controller_get_diagnostics");
const stopDiagnostics = rpc<[string], ControllerDiagnostics>("controller_stop_diagnostics");
const setGyroSource = rpc<[GyroSource], ControllerStatus>("controller_set_gyro_source");
const releaseControl = rpc<[], ControllerStatus>("controller_release_control");
const setSleepTime = rpc<[number], ControllerStatus>("controller_set_sleep_time");

const sourceOptions: DropdownOption[] = Object.entries(sourceLabels).map(([data, label]) => ({ data, label }));

export const controllerSummary = (status?: ControllerStatus) => {
  if (!status) return "Gyro source and controller diagnostics";
  if (!status.available) return status.reason || "Controller unavailable";
  if (status.recovery_pending) return "An interrupted gyro change needs recovery";
  if (status.conflict) return "Controller control is in use elsewhere";
  if (status.error) return "Controller setting needs attention";
  return status.controlled ? `Gyro: ${sourceLabel(status.gyro_source)}` : "Enable gyro · test gyro and touchpad";
};

const finite = (value: unknown): value is number => typeof value === "number" && Number.isFinite(value);
const notify = (body: string) => toaster.toast({ title: "Gyro & touchpad", body });

/** firmware: controller firmware updates apply to this console (get_version.device.firmware). */
export const ControllerPage: FC<{ visible: boolean; firmware?: boolean }> = ({ visible, firmware = true }) => {
  const [status, setStatus] = useState<ControllerStatus | null>(null);
  const [diagnostics, setDiagnostics] = useState<ControllerDiagnostics | null>(null);
  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [testError, setTestError] = useState("");
  const [details, setDetails] = useState(false);
  const mounted = useRef(false);
  const isVisible = useRef(visible);
  const generation = useRef(0);
  const statusRevision = useRef(0);
  const statusRead = useRef<Promise<ControllerStatus | undefined> | null>(null);
  const refreshAfterRead = useRef(false);
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
        setTestError("The stop request was not confirmed. The test ends by itself within seconds.");
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
    const request = (async () => {
      if (mutations.pending) await mutations.tail;
      if (!mounted.current || !isVisible.current || revision !== statusRevision.current) return;
      return getControllerStatus();
    })();
    statusRead.current = request;
    try {
      const next = await request;
      if (next && mounted.current && isVisible.current && revision === statusRevision.current) {
        setStatus(next);
        setError("");
      }
    } catch (failure) {
      if (mounted.current && isVisible.current && revision === statusRevision.current) {
        setError(errorText(failure, "") || "Controller status is unavailable. Retrying while this page is open.");
      }
    } finally {
      if (statusRead.current === request) statusRead.current = null;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refresh(); }
    }
  }, []);

  useEffect(() => {
    if (!visible) {
      stop("Test stopped when this page was hidden.");
      return;
    }
    if (statusRead.current) refreshAfterRead.current = true;
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
        setTestError(errorText(failure, "") || "The controller test could not continue.");
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
    setTestError("");
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
        setTestError(errorText(failure, "") || "The controller test could not start.");
      }
    } finally {
      startInFlight.current = false;
      if (mounted.current) setStarting(false);
    }
  }, [beginPolling]);

  const apply = useCallback(async (operation: () => Promise<ControllerStatus>, message: string,
    problem = (next: ControllerStatus) => next.error || (next.conflict ? next.reason || "Another controller configuration is active." : "")) => {
    // A native dropdown invokes its selection while QAM can still be hidden.
    // Visibility gates polling, not the user's explicit selection callback.
    if (!mounted.current || saving.current || startInFlight.current || stopInFlight.current || token.current) return;
    saving.current = true;
    const previousRead = statusRead.current;
    const activeGeneration = generation.current;
    statusRevision.current += 1;
    setBusy(true);
    setError("");
    const fail = (text: string) => { setError(text); notify(text); };
    const run = async () => {
      try {
        await previousRead?.catch(() => undefined);
        const next = await operation();
        if (!mounted.current || generation.current !== activeGeneration) return;
        setStatus(next);
        const text = problem(next);
        if (text) fail(text);
        else notify(message);
      } catch (failure) {
        if (mounted.current && generation.current === activeGeneration) {
          fail(errorText(failure, "") || "The change could not be confirmed.");
        }
      } finally {
        mutations.done();
        saving.current = false;
        if (mounted.current && generation.current === activeGeneration) setBusy(false);
      }
    };
    await mutations.push(run);
  }, []);

  if (!status) return <>
    {statusPlaceholder({ title: "Controller", label: "Status unavailable", error, onRetry: () => void refresh(), retryLabel: "Check Again" })}
    {firmware && <FirmwarePanel visible={visible} />}
  </>;

  const testing = starting || stopping || !!diagnostics?.active;
  const blocked = busy || testing;
  const remaining = diagnostics?.active && finite(diagnostics.remaining_s) ? Math.ceil(diagnostics.remaining_s) : null;
  const physical = diagnostics?.physical.sample, steam = diagnostics?.virtual.sample;

  return <>
    <PanelSection title="Gyro & touchpad">
      {status.recovery_pending ? infoRow("A controller change didn't finish", "Restore the previous settings first.")
        : status.conflict ? infoRow("Another app changed the controller settings", status.reason || "Restore the previous settings to take them back.")
        : !status.available && infoRow("Gyro not available", status.reason || "The controller was not detected.")}
      <PanelSectionRow>
        <DropdownItem
          label="Gyro source"
          description={busy ? "Applying…" : (error || status.error) ? (error || status.error)
            : sourceNote(status)}
          strDefaultLabel={sourceLabel(status.gyro_source)}
          selectedOption={status.gyro_source}
          rgOptions={sourceOptions}
          disabled={busy || sourceLocked(status, testing)}
          onChange={(option) => {
            const source = String(option.data);
            if (!(source in sourceLabels)) return;
            void apply(() => setGyroSource(source as GyroSource), `Gyro source: ${sourceLabels[source as GyroSource]}.`);
          }}
        />
      </PanelSectionRow>
      {(status.recovery_pending || status.conflict) && <PanelSectionRow>
        <ButtonItem layout="below" disabled={blocked} onClick={() => void apply(releaseControl, "Previous controller settings restored.")}>
          Restore previous controller settings
        </ButtonItem>
      </PanelSectionRow>}
      <PanelSectionRow>
        <ButtonItem
          layout="below"
          description={testError || (diagnostics && !diagnostics.active && diagnostics.reason)
            || "Move the controllers and touch the touchpad for 30 s to see live readings."}
          disabled={busy || starting || stopping || (!diagnostics?.active && !status.physical)}
          onClick={() => diagnostics?.active ? stop() : void start()}
        >
          {starting ? "Starting test…" : stopping ? "Stopping test…"
            : diagnostics?.active ? `Stop test${remaining !== null ? ` · ${remaining} s left` : ""}` : "Start controller test"}
        </ButtonItem>
      </PanelSectionRow>
      {diagnostics && <>
        {infoRow("Left gyro", `${vectorText(physical?.gyro_left)} · ${streamText(diagnostics.physical)}`)}
        {infoRow("Right gyro", vectorText(physical?.gyro_right))}
        {infoRow("Steam gyro", `${vectorText(steam?.gyro)} · ${streamText(diagnostics.virtual)}`)}
        {infoRow("Touchpad", `Physical: ${touchText(physical?.touchpad)} · Steam: ${touchText(steam?.touchpad)}`)}
      </>}
      {detailsRows(details, () => setDetails(value => !value), [
        ["Motion route", sourceLabel(status.applied_source)],
        ["Gyro reporting", reportingText(status)],
        ["Physical controller", status.physical ? status.physical.path : "—"],
        ["Steam controller", status.virtual ? status.virtual.path : "—"],
      ])}
    </PanelSection>
    {sleepShown(status) && <PanelSection title={SLEEP_TITLE}>
      <PanelSectionRow>
        <DropdownItem
          label={SLEEP_LABEL}
          description={busy ? "Applying…" : sleepNote(status)}
          strDefaultLabel={SLEEP_DEFAULT}
          selectedOption={sleepValue(status)}
          rgOptions={sleepOptions(status).map(({ id, label }) => ({ data: id, label }))}
          disabled={blocked}
          onChange={(option) => {
            const minutes = option.data;
            if (!sleepChange(status, minutes)) return;
            void apply(() => setSleepTime(minutes), `Controller sleep: ${sleepText(minutes)}.`, (next) => next.sleep?.error || "");
          }}
        />
      </PanelSectionRow>
    </PanelSection>}
    {firmware && <FirmwarePanel visible={visible} />}
  </>;
};
