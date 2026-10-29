// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk


import { callable } from "@decky/api";
import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";
import { DEVICE_REPORT_DESCRIPTION, DEVICE_REPORT_TITLE, type DeviceReport, deviceReportResult } from "../desktop/native/updates.mjs";
import { homePath } from "./about";
import { settle, type Result } from "./result";
import { infoRow } from "./ui";

const saveDeviceReport = callable<[], Result<DeviceReport>>("save_device_report");

/** About: a read-only hardware report for bug reports, shown on every console. */
export const DeviceReportSection: FC<{ home?: string }> = ({ home = "" }) => {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const mounted = useRef(true);
  const running = useRef(false);  // A second click before the re-render must not start another report.
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const save = async () => {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    const answer = await settle(saveDeviceReport());
    // The full path names the user: inside the home folder it shows as ~/…
    const saved = answer.ok && answer.state?.path ? { ...answer.state, path: homePath(answer.state.path, home) } : answer.state;
    const text = deviceReportResult(answer.ok ? saved : answer);
    running.current = false;
    if (mounted.current) { setMessage(text); setBusy(false); }
  };
  return <PanelSection title="Bug reports">
    {infoRow(DEVICE_REPORT_TITLE, DEVICE_REPORT_DESCRIPTION)}
    <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={() => void save()}>
      {busy ? "Saving report…" : DEVICE_REPORT_TITLE}
    </ButtonItem></PanelSectionRow>
    {message && infoRow("Device report", <span style={{ overflowWrap: "anywhere" }}>{message}</span>)}
  </PanelSection>;
};
