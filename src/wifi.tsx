// SPDX-License-Identifier: BSD-3-Clause

import { toaster, useQuickAccessVisible } from "@decky/api";
import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { confirmAction, detailsRows, infoRow, statusPlaceholder, rpc } from "./shared";
import {
  actionShown, backendName, bandNote, bandOptions, canRun, CHANGED, CONFIRMATIONS, driverText, frequencyText, linkText, networkName,
  operationVerified, POLICY_ACTIONS, savedPolicy, UNVERIFIED, wifiNotice, type Policy, type WifiAction, type WifiStatus,
} from "../desktop/native/wifi.mjs";

export { wifiSummary, type WifiStatus } from "../desktop/native/wifi.mjs";

interface WifiResult {
  success: boolean;
  error?: string;
  message?: string;
  detail?: string;
  reconnected?: boolean;
  frequency?: number | null;
}

export const getWifiStatus = rpc<[], WifiStatus>("wifi_get_status");
const setBandPolicy = rpc<[mode: Policy, allowUnverifiedScan?: boolean], WifiResult>("wifi_set_band_policy");
const rescanAndReconnect = rpc<[targetBand?: "high" | "six"], WifiResult>(
  "wifi_rescan_and_reconnect",
);

const resultMessage = (result: WifiResult) =>
  [result.message, result.detail].filter(Boolean).join(" — ") ||
  "The operation could not be verified.";
const notify = (body: string) => toaster.toast({ title: "Wi-Fi", body });

export const WifiPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const inFlight = useRef<Promise<WifiStatus> | null>(null);
  const refreshAfterRead = useRef(false);
  const operationInFlight = useRef(false);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
  const latestStatus = useRef<WifiStatus | null>(null);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);
  const [status, setStatus] = useState<WifiStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [statusError, setStatusError] = useState("");
  const [details, setDetails] = useState(false);

  const refresh = useCallback(async (verifyOperation = false): Promise<WifiStatus | null> => {
    if (!mounted.current || !isVisible.current || inFlight.current || (operationInFlight.current && !verifyOperation)) return null;
    const revision = readRevision.current;
    const request = getWifiStatus();
    inFlight.current = request;
    try {
      const next = await request;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return null;
      latestStatus.current = next.success ? next : null;
      setStatus(next.success ? next : null);
      setStatusError(next.success ? "" : next.message ?? "Wi-Fi status is unavailable.");
      return next.success ? next : null;
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        latestStatus.current = null; setStatus(null);
        setStatusError("Wi-Fi status is unavailable. Retrying while this page is open.");
      }
      return null;
    } finally {
      if (inFlight.current === request) inFlight.current = null;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refresh(); }
    }
  }, []);

  useEffect(() => {
    if (!visible) {
      latestStatus.current = null; setStatus(null);
      setError(""); setStatusError("");
      return;
    }
    if (inFlight.current) refreshAfterRead.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); readRevision.current += 1; latestStatus.current = null; };
  }, [refresh, visible]);

  // The band menu and its confirmations take focus from the Quick Access menu, which clears the
  // status, so they act on the status they showed (`seen`) unless a newer read contradicts it.
  const run = useCallback(
    async (action: WifiAction, operation: () => Promise<WifiResult>, seen = latestStatus.current) => {
      if (!mounted.current || operationInFlight.current) return;
      const before = latestStatus.current ?? seen;
      if (!canRun(before, action)) {
        if (latestStatus.current && seen !== latestStatus.current) notify(CHANGED);
        return;
      }
      readRevision.current += 1;
      operationInFlight.current = true;
      setBusy(true);
      setError("");
      let result: WifiResult | undefined;
      const fail = (message: string) => { setError(message); notify(message); };
      try {
        result = await operation();
        if (!mounted.current) return;
        if (!result.success) fail(resultMessage(result));
      } catch {
        if (mounted.current) fail("The backend call ended before the result could be verified.");
      } finally {
        await inFlight.current?.catch(() => undefined);
        const verified = await refresh(true);
        if (mounted.current && isVisible.current && result?.success) {
          if (operationVerified(action, before, verified)) notify(resultMessage(result));
          else fail(UNVERIFIED);
        }
        operationInFlight.current = false;
        if (mounted.current) setBusy(false);
      }
    },
    [refresh],
  );

  if (!status) return statusPlaceholder({ label: "Wi-Fi status unavailable", error: statusError, onRetry: () => void refresh() });

  const policy = savedPolicy(status);
  const disabled = (action: WifiAction) => busy || !canRun(status, action);
  const options = bandOptions(status).map(({ id, label }) => ({ data: id, label }));
  const choose = (id: Policy, seen: WifiStatus) => {
    const known = latestStatus.current ?? seen;
    if (id === savedPolicy(known) || !canRun(known, POLICY_ACTIONS[id])) return;
    const ask = id === "five_six_only" || id === "six_ghz_only" ? CONFIRMATIONS[id] : null;
    const apply = () => void run(POLICY_ACTIONS[id], () => ask ? setBandPolicy(id, id === "five_six_only") : setBandPolicy(id), known);
    if (ask) confirmAction(ask.title, ask.text, ask.ok, apply);
    else apply();
  };
  const notice = wifiNotice(status);

  return (
    <PanelSection title="Wi-Fi">
      {notice && infoRow(notice[0], notice[1])}
      {infoRow(networkName(status), busy ? "Checking the connection…" : linkText(status) || undefined)}
      {actionShown(status, "band") && <PanelSectionRow>
        <DropdownItem label="Band" description={error || (busy ? "Applying…" : bandNote(status))} selectedOption={policy} rgOptions={options}
          disabled={busy || options.length < 2} onChange={option => choose(option.data as Policy, status)} />
      </PanelSectionRow>}
      {actionShown(status, "high") && <PanelSectionRow>
        <ButtonItem layout="below" disabled={disabled("high")} onClick={() => void run("high", rescanAndReconnect)}>
          Reconnect to 5/6 GHz
        </ButtonItem>
      </PanelSectionRow>}
      {actionShown(status, "six") && <PanelSectionRow>
        <ButtonItem layout="below" description="Then roams normally again." disabled={disabled("six")}
          onClick={() => void run("six", () => rescanAndReconnect("six"))}>
          Connect to 6 GHz once
        </ButtonItem>
      </PanelSectionRow>}
      {detailsRows(details, () => setDetails(value => !value), [
        ["Backend", backendName(status)],
        ["Driver", driverText(status)],
        ["Frequency", frequencyText(status)],
      ])}
    </PanelSection>
  );
};
