// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  DropdownItem,
  DropdownOption,
  PanelSection,
  PanelSectionRow,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import type { Result } from "./result";
import { mutationQueue, rpc } from "./shared";
import { infoRow, notify as toast, statusPlaceholder } from "./ui";
import { BUTTONS, enableNote, mappingEditable } from "../desktop/native/remap.mjs";

interface ActionOption {
  id: string;
  label: string;
}

export interface RemapStatus {
  supported: boolean;
  enabled: boolean;
  active: boolean;
  drift: boolean;
  desktop_action: string;
  page_action: string;
  actions: ActionOption[];
  reason?: string;
  error?: string;
}

// A change answers D2 itself: the new status, also with a refusal (a refusal before any change has none).
type RemapResult = Result<RemapStatus>;
const UNCONFIRMED = "The mapping could not be confirmed. Check the buttons before trying again.";

// Accepted edits survive page close. Every later menu/page snapshot follows them.
const mutations = mutationQueue();
const readRemapStatus = rpc<[], RemapStatus>("remap_get_status");
export const getRemapStatus = async () => {
  if (mutations.pending) await mutations.tail;
  return readRemapStatus();
};
const setRemapEnabled = callable<[boolean], RemapResult>("remap_set_enabled");
const setRemapAction = callable<[string, string], RemapResult>("remap_set_action");
const restoreRemapDefaults = callable<[], RemapResult>("remap_restore_defaults");

// Every status lists all actions with labels, and saved actions are always one of them.
const actionLabel = (status: RemapStatus | undefined, action: string) =>
  status?.actions.find((candidate) => candidate.id === action)?.label ?? "Unknown";

export const remapSummary = (status?: RemapStatus) => {
  if (!status?.supported) return status?.reason || "Desktop and Page button actions";
  if (!status.enabled) return "Desktop and Page · system defaults";
  return `Desktop: ${actionLabel(status, status.desktop_action)} · Page: ${actionLabel(status, status.page_action)}`;
};

export const RemapPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const [status, setStatus] = useState<RemapStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The control whose last change failed shows that error until the next change.
  const [failed, setFailed] = useState<{control: string; text: string} | null>(null);
  const pending = useRef(0);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
  const readInFlight = useRef(false);
  const refreshAfterRead = useRef(false);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || pending.current || readInFlight.current) return;
    readInFlight.current = true;
    const revision = readRevision.current;
    try {
      if (mutations.pending) await mutations.tail;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      const next = await getRemapStatus();
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      setStatus(next);
      setError(next.error || "");
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current)
        setError("Button mapping status is unavailable. Retrying while this page is open.");
    } finally {
      readInFlight.current = false;
      if (refreshAfterRead.current) { refreshAfterRead.current = false; void refresh(); }
    }
  }, []);

  useEffect(() => {
    if (!visible) return;
    if (readInFlight.current) refreshAfterRead.current = true;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); readRevision.current += 1; };
  }, [refresh, visible]);

  const enqueue = useCallback((operation: () => Promise<RemapResult>, message: string, control: string) => {
    if (!mounted.current) return Promise.resolve();
    readRevision.current += 1;
    pending.current += 1;
    const run = async () => {
      let needsRefresh = false;
      if (mounted.current) { setBusy(true); setError(""); setFailed(null); }
      const fail = (text: string) => { setFailed({control, text}); toast("Button mapping", text); };
      try {
        const { ok, error, state } = await operation();
        if (!mounted.current) return;
        if (state) setStatus(state);
        else needsRefresh = true;
        if (ok) toast("Button mapping", message);
        else fail(state ? error || "The controller did not confirm the new mapping." : UNCONFIRMED);
      } catch {
        needsRefresh = true;
        if (mounted.current) fail(UNCONFIRMED);
      } finally {
        mutations.done();
        pending.current -= 1;
        if (mounted.current && !pending.current) {
          setBusy(false);
          if (needsRefresh) { // Drop the optimistic dropdown value; a read already in flight predates this write.
            if (readInFlight.current) refreshAfterRead.current = true;
            void refresh();
          }
        }
      }
    };
    return mutations.push(run);
  }, [refresh]);

  if (!status) return statusPlaceholder({ label: "Button mapping status unavailable", error, onRetry: () => void refresh() });

  const options: DropdownOption[] = status.actions.map((action) => ({
    data: action.id,
    label: action.label,
  }));
  const blocked = busy || !status.supported;

  const failure = (control: string) => failed?.control === control ? failed.text : undefined;
  const dropdown = (button: "desktop" | "page", value: string, label: string, description: string) => (
    <PanelSectionRow key={button}>
      <DropdownItem
        label={label}
        description={failure(button) ?? description}
        strDefaultLabel={actionLabel(status, value)}
        selectedOption={value}
        rgOptions={options}
        disabled={busy || !mappingEditable(status)}
        onChange={(option) => {
          if (!mounted.current) return;
          const action = String(option.data);
          setStatus((current) => current ? {
            ...current,
            [`${button}_action`]: action,
          } : current);
          void enqueue(
            () => setRemapAction(button, action),
            `${label}: ${actionLabel(status, action)}.`,
            button,
          );
        }}
      />
    </PanelSectionRow>
  );

  return <PanelSection title="Button mapping">
    {!status.supported ? infoRow("Button mapping unavailable", status.reason || "A compatible controller was not detected.")
      : error && infoRow("Button mapping status not updated", error)}
    <PanelSectionRow>
      <ToggleField
        label="Enable button mapping"
        description={failure("enabled") ?? enableNote(status)}
        checked={status.enabled}
        disabled={blocked}
        onChange={(enabled) => void enqueue(
          () => setRemapEnabled(enabled),
          enabled ? "Button mapping on." : "Button mapping off. The previous controller profile is back.",
          "enabled",
        )}
      />
    </PanelSectionRow>
    {status.supported && <>
      {BUTTONS.map(({ id, label, note }) => dropdown(id, status[`${id}_action`], label, note))}
      {/* The page's last row: always enabled; the mutation queue orders presses. */}
      <PanelSectionRow>
        <ButtonItem layout="below" description={failure("defaults")}
          onClick={() => void enqueue(() => restoreRemapDefaults(), "Defaults restored.", "defaults")}>
          Restore defaults
        </ButtonItem>
      </PanelSectionRow>
    </>}
  </PanelSection>;
};
