// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGo2BrightnessFix

import { callable, useQuickAccessVisible } from "@decky/api";
import { PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { detailsRows, infoRow, statusPlaceholder } from "./shared";
import { EDID_FIX_LABEL, EDID_FIX_NOTE, edidStatus, steamosNotice } from "../desktop/native/display.mjs";

interface State {
  settings_error?: string;
  steamos_update_needed?: boolean;
  edid_fix: boolean;
  edid_patched: boolean;
  edid_reason: string;
  edid_game_nits: number;
}

const getState = callable<[], State>("display_get_state");
const setEdidFix = callable<[boolean], State>("display_set_edid_fix");

export const DisplayPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const [state, setState] = useState<State | null>(null);
  const [statusError, setStatusError] = useState("");
  const [busy, setBusy] = useState(false);
  const [details, setDetails] = useState(false);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const writing = useRef(false);
  const readInFlight = useRef(false);
  const refreshAfterRead = useRef(false);
  const readRevision = useRef(0);
  const latestState = useRef<State | null>(null);
  const statusTrusted = useRef(false);
  const visitRevision = useRef(0);
  if (isVisible.current !== visible) {
    statusTrusted.current = false;
    visitRevision.current += 1;
  }
  const acceptState = useCallback((next: State) => {
    latestState.current = next;
    statusTrusted.current = isVisible.current;
    setStatusError("");
    setState(next);
  }, []);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || writing.current || readInFlight.current) return;
    readInFlight.current = true;
    const revision = readRevision.current;
    try {
      const next = await getState();
      if (mounted.current && isVisible.current && revision === readRevision.current) acceptState(next);
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current) {
        statusTrusted.current = false;
        setStatusError("Display status is unavailable. Retrying while this page is open.");
      }
    } finally {
      readInFlight.current = false;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refresh(); }
    }
  }, [acceptState]);

  // Only poll while the panel is actually on screen. The backend keeps working
  // either way - this is just what the user sees.
  useEffect(() => {
    if (!visible) return;
    if (readInFlight.current) refreshAfterRead.current = true;
    void refresh();
    const id = setInterval(refresh, 1000);
    return () => { clearInterval(id); readRevision.current += 1; };
  }, [visible, refresh]);

  const toggle = useCallback(async (value: boolean) => {
    if (!mounted.current || writing.current || !isVisible.current || !statusTrusted.current) return;
    writing.current = true; readRevision.current += 1; setBusy(true);
    const visit = visitRevision.current;
    if (latestState.current) acceptState({ ...latestState.current, edid_fix: value });
    try {
      const next = await setEdidFix(value);
      if (mounted.current && visit === visitRevision.current) acceptState(next);
    } catch {
      statusTrusted.current = false;
      if (mounted.current) setStatusError("Display status is unavailable. Confirming the result of the previous change.");
    } finally {
      writing.current = false;
      if (mounted.current) setBusy(false);
      if (!statusTrusted.current) {
        if (readInFlight.current) refreshAfterRead.current = true;
        else void refresh();
      }
    }
  }, [acceptState, refresh]);

  if (!state || !statusTrusted.current)
    return statusPlaceholder({ label: "Display status unavailable", error: statusError, onRetry: () => void refresh() });

  const notice = steamosNotice(state);
  return <PanelSection title="Display">
    {state.settings_error && infoRow("Display settings unavailable", state.settings_error)}
    {notice && infoRow("SteamOS update needed", notice)}
    <PanelSectionRow>
      <ToggleField
        label={EDID_FIX_LABEL}
        description={`${EDID_FIX_NOTE} ${edidStatus(state)}.`}
        checked={state.edid_fix}
        disabled={busy || !!state.settings_error}
        onChange={(v) => void toggle(v)}
      />
    </PanelSectionRow>
    {detailsRows(details, () => setDetails(value => !value), [["HDR info", state.edid_reason || "—"]])}
  </PanelSection>;
};
