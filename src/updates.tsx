// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable } from "@decky/api";
import { ButtonItem, PanelSection, PanelSectionRow } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";
import { type UpdateCheck, updateStatus } from "../desktop/native/updates.mjs";
import { errorText, infoRow } from "./shared";

interface UpdateDownload {
  success: boolean;
  path?: string;
  version?: string;
  error?: string;
}

type UpdateState = {
  busy: "check" | "download" | null;
  check?: UpdateCheck;
  download?: UpdateDownload;
  error?: string;
};

const checkUpdates = callable<[], UpdateCheck>("updates_check");
const downloadUpdate = callable<[string], UpdateDownload>("updates_download");
const listeners = new Set<(state: UpdateState) => void>();
let state: UpdateState = { busy: null };
let generation = 0;
let active = false;

function publish(next: UpdateState) {
  state = next;
  for (const listener of listeners) listener(state);
}

// About can unmount while an explicit request is running. Its result belongs to
// this plugin session, not to that particular page instance.
export function startUpdates() {
  generation++;
  active = true;
  publish({ busy: null });
}

export function stopUpdates() {
  generation++;
  active = false;
  publish({ busy: null });
}

export const UpdateSection: FC<{ currentVersion: string }> = ({ currentVersion }) => {
  const [view, setView] = useState<UpdateState>(() => state);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    listeners.add(setView);
    setView(state);
    return () => { mounted.current = false; listeners.delete(setView); };
  }, []);

  const run = async (kind: "check" | "download", expected?: string) => {
    if (!mounted.current || !active || state.busy !== null) return;
    if (kind === "download" && (!state.check?.update_available || !state.check.download_available
      || !expected || state.check.latest_version !== expected)) return;
    const requestGeneration = generation;
    publish({ ...state, busy: kind, error: undefined });
    try {
      if (kind === "check") {
        const result = await checkUpdates();
        if (!active || requestGeneration !== generation) return;
        if (!result.success) throw new Error(result.error || "Could not check GitHub releases. Try again.");
        publish({ ...state, check: result, error: result.error });
      } else {
        const result = await downloadUpdate(expected!);
        if (!active || requestGeneration !== generation) return;
        if (!result.success || !result.path || result.version !== expected) {
          throw new Error(result.error || "Could not confirm the downloaded ZIP. Try again.");
        }
        publish({ ...state, download: result });
      }
    } catch (error) {
      if (active && requestGeneration === generation) {
        console.warn(`[legiongo2companion] updates.${kind}_failed`);
        publish({ ...state, error: errorText(error, "") || "The request failed. Try again." });
      }
    } finally {
      if (active && requestGeneration === generation) publish({ ...state, busy: null });
    }
  };

  const installed = view.check?.current_version || currentVersion;
  const [label, description] = updateStatus(view.check, installed);
  const canDownload = view.check?.update_available && view.check.download_available && view.check.latest_version;
  return <PanelSection title="Updates">
    {infoRow(label, description)}
    <PanelSectionRow><ButtonItem layout="below" disabled={view.busy !== null} onClick={() => void run("check")}>
      {view.busy === "check" ? "Checking GitHub…" : "Check for updates"}
    </ButtonItem></PanelSectionRow>
    {canDownload && <PanelSectionRow><ButtonItem layout="below" disabled={view.busy !== null} onClick={() => void run("download", view.check!.latest_version)}>
      {view.busy === "download" ? "Downloading ZIP…" : `Download ${view.check!.latest_version}`}
    </ButtonItem></PanelSectionRow>}
    {view.error && infoRow("Could not complete", view.error)}
    {view.download?.path && infoRow(`${view.download.version} downloaded`,
      <span style={{ overflowWrap: "anywhere" }}>
        {`Saved to ${view.download.path}. Install it from Decky settings → Developer → Install plugin from ZIP.`}
      </span>)}
  </PanelSection>;
};
