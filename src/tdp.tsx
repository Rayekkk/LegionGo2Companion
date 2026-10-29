// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP

import { ButtonItem, PanelSection, PanelSectionRow, Spinner } from "@decky/ui";
import { FC } from "react";
import { infoRow } from "./shared";
import { AppWatcher } from "./tdp_watcher";
import { CpuPowerControlsSection } from "./tdp_cpu";
import { useTdpState } from "./tdp_state";
import { customLimitsSection, moreSection, noticeSection, powerSection } from "./tdp_sections";

export const TdpPage: FC<{ visible?: boolean }> = ({ visible: tabShown = true }) => {
  const s = useTdpState(tabShown);
  const { ready, setupErr, initializing, initialize, enabled, preset, profilePending, profileError, game, perGame,
    acSeparate, editingAc, acOnline, onCpuBusyChange, setCpuNote } = s;

  if (!ready && setupErr) return (
    <PanelSection title="TDP status unavailable">
      {infoRow("Error", setupErr)}
      <PanelSectionRow><ButtonItem disabled={initializing} onClick={() => void initialize()}>
        Retry status
      </ButtonItem></PanelSectionRow>
    </PanelSection>
  );

  if (!ready) return (
    <PanelSection title="Initializing...">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  return (
    <>
      {noticeSection(s)}

      {powerSection(s)}

      {enabled && preset === "custom" && customLimitsSection(s)}

      {!profilePending && !profileError && <CpuPowerControlsSection
        key={`${enabled}:${game?.appId ?? ""}:${perGame}:${acSeparate && editingAc}`}
        appId={enabled && perGame && game ? game.appId : ""}
        acProfile={!!(enabled && perGame && game && acSeparate && editingAc)}
        expectedAppId={game?.appId ?? ""}
        powerSource={acOnline}
        onBusyChange={onCpuBusyChange}
        onCompatibilityNote={setCpuNote}
      />}

      {moreSection(s)}
    </>
  );
};

// ── Plugin entry point ─────────────────────────────────────────────────────────

export const startTdpWatcher = () => AppWatcher.start();
export const stopTdpWatcher = () => AppWatcher.stop();
