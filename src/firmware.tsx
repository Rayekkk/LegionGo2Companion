// SPDX-License-Identifier: BSD-3-Clause
import { callable, toaster } from "@decky/api";
import { ButtonItem, ConfirmModal, Field, PanelSection, PanelSectionRow, showModal } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";
import { errorText, infoRow } from "./shared";
import { BUSY_LABELS as busyPhases, installConfirm, PHASE_LABELS as phaseLabel, RISK_NOTICE } from "../desktop/native/firmware.mjs";

type FirmwareComponent = { id: string; label: string; installed: string; available: string; newer: boolean; downloaded: boolean };
type FirmwareStatus = { phase: string; message: string; error: string; component: string; install_enabled: boolean;
  install_reason: string; checked_at: string; token: string; components: FirmwareComponent[] };
const getStatus = callable<[], FirmwareStatus>("firmware_get_status");
const checkUpdates = callable<[], FirmwareStatus>("firmware_check");
const downloadUpdate = callable<[], FirmwareStatus>("firmware_download");
const installUpdate = callable<[string], FirmwareStatus>("firmware_install");
const checkPhases = ["idle", "up_to_date", "installed", "failed"];
const canInstall = (value: FirmwareStatus) => value.phase === "downloaded" && value.install_enabled;
const has = (table: object, key: unknown) => typeof key === "string" && Object.prototype.hasOwnProperty.call(table, key);
const knownStatus = (value: FirmwareStatus) => !!value && has(phaseLabel, value.phase) &&
  [value.message, value.error, value.token, value.install_reason].every(v => v === undefined || typeof v === "string") &&
  (value.components === undefined || Array.isArray(value.components));
const actionLabel = (value: FirmwareStatus) => (has(busyPhases, value.phase) ? busyPhases[value.phase] : "") ||
  (value.phase === "available" ? "Download" : canInstall(value) ? "Install" : "Check for updates");
export const FirmwarePanel: FC<{ visible: boolean; allowUpdates?: boolean }> = ({ visible, allowUpdates = true }) => {
  const [state, setState] = useState<FirmwareStatus | null>(null);
  const [error, setError] = useState("");
  // A refused step stays visible until the next click; the status poll clears only `error`.
  const [stepError, setStepError] = useState("");
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
          invalidate(); setError(errorText(failure, "Firmware status is unavailable."));
        }
      } finally { currentRead.current = null; inFlight.current = false; }
      if (alive) timer = setTimeout(() => void poll(), 2000);
    };
    void poll();
    return () => { alive = false; if (timer) clearTimeout(timer); invalidate(); };
  }, [visible]);
  // One explicit step per click; it runs only if the backend is still in the phase the user saw.
  const begin = async (phase: string, run: () => Promise<FirmwareStatus>) => {
    const current = (value: FirmwareStatus | null) => value?.phase === phase;
    if (!allowUpdates || mutating.current || !freshRef.current || performance.now() >= expiresAt.current ||
        !current(statusRef.current) || !mounted.current || !shown.current) return;
    mutating.current = true; revision.current++;
    invalidate();
    setSubmitting(true); setError(""); setStepError("");
    try {
      const pending = currentRead.current;
      if (pending) {
        const sampledAt = readStarted.current;
        const latest = await pending;
        if (!mounted.current || !shown.current) return;
        if (!adopt(latest, sampledAt)) return;
        if (!current(latest)) return;
      }
      if (!mounted.current || !shown.current) return;
      const sampledAt = performance.now();
      const result = await run(); if (mounted.current && shown.current) adopt(result, sampledAt);
    }
    catch (failure) { if (mounted.current) { console.warn("[legiongo2companion] firmware.step_failed"); invalidate(); setStepError(errorText(failure, "The firmware step failed. Wait for a fresh status before retrying.")); } }
    finally { mutating.current = false; if (mounted.current) setSubmitting(false); }
  };
  // The confirmation takes focus from the Quick Access menu, so the panel is usually hidden and
  // its status stale when OK is pressed. The token pins what the dialog showed; the backend
  // refuses any other check, versions or images.
  const install = async (token: string) => {
    if (!allowUpdates || mutating.current) return;
    const latest = statusRef.current;
    if (latest && (latest.phase !== "downloaded" || latest.token !== token)) {
      toaster.toast({ title: "Controller firmware", body: "The download changed. Check for updates again." });
      return;
    }
    mutating.current = true; revision.current++;
    invalidate();
    setSubmitting(true); setError(""); setStepError("");
    try {
      const sampledAt = performance.now();
      const result = await installUpdate(token); if (mounted.current) adopt(result, sampledAt);
    } catch (failure) {
      const text = errorText(failure, "The firmware update did not start. Check for updates again.");
      console.warn("[legiongo2companion] firmware.step_failed");
      toaster.toast({ title: "Controller firmware", body: text });
      if (mounted.current) { invalidate(); setStepError(text); }
    } finally { mutating.current = false; if (mounted.current) setSubmitting(false); }
  };
  const confirmInstall = () => {
    const seen = statusRef.current;
    if (!seen || seen.phase !== "downloaded" || !seen.install_enabled || !freshRef.current) return;
    const ask = installConfirm(seen);
    showModal(<ConfirmModal strTitle={ask.title} strOKButtonText={ask.ok}
      strDescription={<div style={{ whiteSpace: "pre-line" }}>{ask.text}</div>}
      onOK={() => void install(seen.token)} />);
  };
  const act = () => {
    const phase = statusRef.current?.phase || "";
    if (statusRef.current && canInstall(statusRef.current)) confirmInstall();
    else if (phase === "available") void begin(phase, () => downloadUpdate());
    // With the install kill switch on, a finished download offers Check again instead of a dead end.
    else if (checkPhases.includes(phase) || phase === "downloaded") void begin(phase, () => checkUpdates());
  };
  const phase = state?.phase || "";
  const installBlocked = !!state && phase === "downloaded" && !state.install_enabled;
  const rows = (state?.components || []).filter(c => c && (c.installed || c.available));
  return <PanelSection title="Controller firmware">
    <PanelSectionRow><div style={{ background: "rgba(236, 99, 112, 0.12)", border: "1px solid rgba(236, 99, 112, 0.45)", borderRadius: 10, padding: "4px 12px" }}>
      <Field focusable label={<span style={{ color: "#ff9ca6", fontWeight: 600 }}>{RISK_NOTICE.title}</span>}
        description={<span style={{ color: "#ff9ca6" }}>{RISK_NOTICE.text}</span>} />
    </div></PanelSectionRow>
    {infoRow(fresh ? phaseLabel[phase] : state ? "Status unavailable" : "Controller firmware",
      fresh ? state?.error || state?.message || "" : "Wait for a fresh firmware status before updating.")}
    {fresh && rows.map(c => <PanelSectionRow key={c.id}><Field focusable label={c.label}
      description={`Installed ${c.installed || "—"} · Available ${c.available || "—"}${c.newer ? " · newer" : ""}`} /></PanelSectionRow>)}
    {fresh && installBlocked && infoRow("Install unavailable", state?.install_reason || "")}
    <PanelSectionRow><ButtonItem layout="below" disabled={!allowUpdates || !fresh || !state || submitting || has(busyPhases, phase)}
      onClick={act}>{state ? actionLabel(state) : "Check for updates"}</ButtonItem></PanelSectionRow>
    {(stepError || error) && infoRow("Firmware status", stepError || error)}
  </PanelSection>;
};
