// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  DropdownItem,
  DropdownOption,
  Field,
  PanelSection,
  PanelSectionRow,
  Spinner,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";

interface ActionOption {
  id: string;
  label: string;
}

export interface RemapStatus {
  success: boolean;
  supported: boolean;
  enabled: boolean;
  active: boolean;
  drift: boolean;
  desktop_action: string;
  page_action: string;
  actions: ActionOption[];
  inputplumber_version?: string;
  profile_name?: string;
  reason?: string;
  error?: string;
}

interface RemapResult {
  success: boolean;
  error?: string;
  status?: RemapStatus;
}

export const getRemapStatus = callable<[], RemapStatus>("remap_get_status");
const setRemapEnabled = callable<[boolean], RemapResult>("remap_set_enabled");
const setRemapAction = callable<[string, string], RemapResult>("remap_set_action");
const restoreRemapDefaults = callable<[], RemapResult>("remap_restore_defaults");

const fallbackLabels: Record<string, string> = {
  default: "Default",
  keyboard: "On-screen keyboard",
  screenshot: "Screenshot",
  steam: "Steam menu",
  quick_access: "Quick access menu",
  show_desktop: "Show desktop",
  alt_tab: "Switch window (Alt+Tab)",
  escape: "Escape",
  enter: "Enter",
  page_up: "Page Up",
  page_down: "Page Down",
  home: "Home",
  end: "End",
  f1: "F1",
  f2: "F2",
  f3: "F3",
  f4: "F4",
  f5: "F5",
  f6: "F6",
  f7: "F7",
  f8: "F8",
  f9: "F9",
  f10: "F10",
  f11: "F11",
  f12: "F12",
  disabled: "Disabled",
};

const actionLabel = (status: RemapStatus | undefined, action: string) =>
  status?.actions.find((candidate) => candidate.id === action)?.label
    ?? fallbackLabels[action]
    ?? "Unknown";

export const remapSummary = (status?: RemapStatus) => {
  if (!status?.supported) return status?.reason || "Desktop and Page button actions";
  if (!status.enabled) return "Desktop and Page · system defaults";
  return `Desktop: ${actionLabel(status, status.desktop_action)} · Page: ${actionLabel(status, status.page_action)}`;
};

export const RemapPage: FC = () => {
  const visible = useQuickAccessVisible();
  const [status, setStatus] = useState<RemapStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const pending = useRef(0);
  const chain = useRef<Promise<void>>(Promise.resolve());
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
  const readInFlight = useRef(false);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || pending.current || readInFlight.current) return;
    readInFlight.current = true;
    const revision = readRevision.current;
    try {
      const next = await getRemapStatus();
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      setStatus(next);
      setError(next.error || "");
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current)
        setError("Button-remapper status is unavailable. Retrying while this page is open.");
    } finally { readInFlight.current = false; }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); readRevision.current += 1; };
  }, [refresh, visible]);

  const enqueue = useCallback((operation: () => Promise<RemapResult>, message: string) => {
    readRevision.current += 1;
    pending.current += 1;
    const run = async () => {
      setBusy(true);
      setNotice("");
      setError("");
      try {
        const result = await operation();
        if (!mounted.current) return;
        if (result.status) setStatus(result.status);
        if (result.success) setNotice(message);
        else setError(result.error || "InputPlumber did not confirm the requested mapping.");
      } catch {
        if (mounted.current) setError("The backend call ended before InputPlumber confirmed the mapping.");
      } finally {
        pending.current -= 1;
        if (mounted.current && !pending.current) setBusy(false);
      }
    };
    const queued = chain.current.then(run, run);
    chain.current = queued.catch(() => undefined);
    return queued;
  }, []);

  if (!status) {
    return <PanelSection><PanelSectionRow><Spinner /></PanelSectionRow></PanelSection>;
  }

  const options: DropdownOption[] = status.actions.map((action) => ({
    data: action.id,
    label: action.label,
  }));
  const blocked = busy || !status.supported;

  const dropdown = (button: "desktop" | "page", value: string, label: string, description: string) => (
    <PanelSectionRow>
      <DropdownItem
        label={label}
        description={description}
        strDefaultLabel={actionLabel(status, value)}
        selectedOption={value}
        rgOptions={options}
        disabled={blocked || !status.enabled}
        onChange={(option) => {
          const action = String(option.data);
          setStatus((current) => current ? {
            ...current,
            [`${button}_action`]: action,
          } : current);
          void enqueue(
            () => setRemapAction(button, action),
            `${label} now uses ${actionLabel(status, action)}.`,
          );
        }}
      />
    </PanelSectionRow>
  );

  return <>
    <PanelSection title="Button remapping">
      <PanelSectionRow>
        <ToggleField
          label="Enable button remapping"
          description="Let Companion configure the two extra buttons through SteamOS InputPlumber. All other controller mappings are preserved."
          checked={status.enabled}
          disabled={blocked}
          onChange={(enabled) => void enqueue(
            () => setRemapEnabled(enabled),
            enabled ? "Button remapping enabled." : "The previous InputPlumber profile was restored.",
          )}
        />
      </PanelSectionRow>
      {!status.supported && (
        <PanelSectionRow>
          <Field label="Remapping unavailable" description={status.reason || "A compatible InputPlumber controller was not detected."} />
        </PanelSectionRow>
      )}
      {status.supported && (
        <PanelSectionRow>
          <Field
            label={status.active ? "Active" : status.enabled ? "Waiting to reapply" : "System defaults"}
            description={status.reason}
          />
        </PanelSectionRow>
      )}
    </PanelSection>

    {status.supported && <PanelSection title="Extra left-controller buttons">
      {dropdown(
        "desktop",
        status.desktop_action,
        "Desktop button",
        "The upper extra button. Default keeps its current SteamOS behavior.",
      )}
      {dropdown(
        "page",
        status.page_action,
        "Page button",
        "The lower extra button. Its system default takes a screenshot.",
      )}
      {status.enabled && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy}
            onClick={() => void enqueue(
              () => restoreRemapDefaults(),
              "Both buttons use their SteamOS defaults.",
            )}
          >
            Restore Default Actions
          </ButtonItem>
        </PanelSectionRow>
      )}
    </PanelSection>}

    {(notice || error) && <PanelSection title={error ? "Could not apply mapping" : "Saved"}>
      <PanelSectionRow>
        <Field label={error ? "InputPlumber kept the last safe profile" : "Mapping confirmed"} description={error || notice} />
      </PanelSectionRow>
    </PanelSection>}

    {status.inputplumber_version && <PanelSection title="System interface">
      <PanelSectionRow>
        <Field label={status.inputplumber_version} description="Companion uses the existing SteamOS remapping service; no additional input daemon is installed." />
      </PanelSectionRow>
    </PanelSection>}
  </>;
};
