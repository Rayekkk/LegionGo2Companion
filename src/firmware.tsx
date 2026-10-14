// SPDX-License-Identifier: BSD-3-Clause
import { callable } from "@decky/api";
import { ButtonItem, Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";

type FirmwareStatus = {
  job: { id: string; phase: string; message: string; error: string };
};
const getStatus = callable<[], FirmwareStatus>("firmware_get_status");
const checkUpdates = callable<[], FirmwareStatus>("firmware_check_updates");
const activePhases = ["checking", "downloading", "validating", "installing", "verifying", "restoring"];
const retryablePhases = ["idle", "succeeded", "up_to_date", "blocked", "failed", "failed_before_write", "stopped"];
const knownStatus = (value: FirmwareStatus) => [value?.job?.id, value?.job?.message, value?.job?.error].every(v => v === undefined || typeof v === "string") &&
  (activePhases.includes(value?.job?.phase) || retryablePhases.includes(value?.job?.phase) || value?.job?.phase === "uncertain");
const phaseLabel = (phase: string) => ({ checking: "Checking for updates", downloading: "Downloading update",
  validating: "Checking update", installing: "Updating controllers", verifying: "Verifying update",
  restoring: "Reconnecting controllers", succeeded: "Controllers updated", up_to_date: "No update available",
  blocked: "Update unavailable", failed: "Update failed", failed_before_write: "Update not started", uncertain: "Update needs attention", stopped: "Check stopped" }[phase] || "Controller firmware");
export const FirmwarePanel: FC<{ visible: boolean; allowUpdates?: boolean }> = ({ visible, allowUpdates = true }) => {
  const [state, setState] = useState<FirmwareStatus | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [fresh, setFresh] = useState(false);
  const mounted = useRef(false);
  const inFlight = useRef(false);
  const mutating = useRef(false);
  const currentRead = useRef<Promise<FirmwareStatus> | null>(null);
  const readStarted = useRef(0);
  const revision = useRef(0);
  const statusRef = useRef<FirmwareStatus | null>(null);
  const unavailable = useRef(false);
  const freshRef = useRef(false);
  const expiresAt = useRef(0);
  const expiry = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const shown = useRef(visible); shown.current = visible;
  const invalidate = () => {
    freshRef.current = false;
    if (expiry.current !== undefined) clearTimeout(expiry.current);
    expiry.current = undefined;
    if (mounted.current) setFresh(false);
  };
  const adopt = (value: FirmwareStatus, sampledAt = performance.now()) => {
    if (!knownStatus(value)) throw new Error("Firmware status is unavailable. Wait for a fresh status before updating.");
    statusRef.current = value; setState(value); invalidate();
    expiresAt.current = sampledAt + 8000;
    const remaining = expiresAt.current - performance.now();
    if (remaining <= 0) return false;
    if (unavailable.current) console.info("[legiongo2companion] firmware.status_restored");
    unavailable.current = false;
    freshRef.current = true; setFresh(true);
    expiry.current = setTimeout(() => { invalidate(); }, remaining);
    return true;
  };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; revision.current++; }; }, []);
  useEffect(() => {
    if (!visible) return;
    let alive = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (inFlight.current || mutating.current) { timer = setTimeout(() => void poll(), 1000); return; }
      inFlight.current = true;
      const generation = revision.current;
      try {
        const sampledAt = performance.now(); readStarted.current = sampledAt;
        const request = getStatus(); currentRead.current = request;
        const result = await request;
        if (alive && mounted.current && generation === revision.current) { adopt(result, sampledAt); setError(""); }
      } catch (failure) {
        if (alive && mounted.current && generation === revision.current) {
          if (!unavailable.current) console.warn("[legiongo2companion] firmware.status_unavailable");
          unavailable.current = true;
          invalidate(); setError(failure instanceof Error ? failure.message : "Firmware status is unavailable.");
        }
      } finally { currentRead.current = null; inFlight.current = false; }
      if (alive) timer = setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => { alive = false; if (timer) clearTimeout(timer); invalidate(); };
  }, [visible]);
  const begin = async () => {
    if (!allowUpdates || mutating.current || !freshRef.current || performance.now() >= expiresAt.current ||
        !retryablePhases.includes(statusRef.current?.job.phase || "") || !mounted.current || !shown.current) return;
    mutating.current = true; revision.current++;
    invalidate();
    setSubmitting(true); setError("");
    try {
      const pending = currentRead.current;
      if (pending) {
        const sampledAt = readStarted.current;
        const latest = await pending;
        if (!mounted.current || !shown.current) return;
        if (!adopt(latest, sampledAt)) return;
        if (!retryablePhases.includes(latest.job.phase)) return;
      }
      if (!mounted.current || !shown.current) return;
      const sampledAt = performance.now();
      const result = await checkUpdates(); if (mounted.current && shown.current) adopt(result, sampledAt);
    }
    catch (failure) { if (mounted.current) { console.warn("[legiongo2companion] firmware.check_failed"); invalidate(); setError(failure instanceof Error ? failure.message : "Firmware check failed. Wait for a fresh status before retrying."); } }
    finally { mutating.current = false; if (mounted.current) setSubmitting(false); }
  };
  const active = activePhases.includes(state?.job.phase || "");
  return <PanelSection title="Controller firmware">
    <PanelSectionRow><div style={{ background: "rgba(236, 99, 112, 0.12)", border: "1px solid rgba(236, 99, 112, 0.45)", borderRadius: 10, padding: "4px 12px" }}>
      <Field focusable label={<span style={{ color: "#ff9ca6" }}>Experimental feature</span>}
        description={<span style={{ color: "#ff9ca6" }}>Use at your own risk.</span>} />
    </div></PanelSectionRow>
    <PanelSectionRow><Field focusable label={fresh ? phaseLabel(state?.job.phase || "idle") : state ? "Status unavailable" : "Controller firmware"}
      description={fresh ? state?.job.error || state?.job.message || "Check for Lenovo updates for your controllers. Available updates are installed together." : "Wait for a fresh firmware status before updating."} /></PanelSectionRow>
    <PanelSectionRow><ButtonItem layout="below" disabled={!allowUpdates || !fresh || !state || active || submitting || !retryablePhases.includes(state.job.phase)} onClick={() => void begin()}>Check updates</ButtonItem></PanelSectionRow>
    {error && <PanelSectionRow><Field focusable label="Firmware status" description={error} /></PanelSectionRow>}
  </PanelSection>;
};
