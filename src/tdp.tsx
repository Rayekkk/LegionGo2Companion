// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP

import { FC, useEffect } from "react";
import { statusPlaceholder } from "./ui";
import { AppWatcher } from "./tdp_watcher";
import { CpuPowerControlsSection } from "./tdp_cpu";
import { useTdpState } from "./tdp_state";
import { customLimitsSection, moreSection, noticeSection, powerSection } from "./tdp_sections";

export const TdpPage: FC<{ visible?: boolean; guardLeave?: (check: (() => string | null) | null) => void }> =
  ({ visible: tabShown = true, guardLeave }) => {
  const s = useTdpState(tabShown);
  // Leaving the page with Custom limits not applied asks first (index.tsx).
  useEffect(() => {
    guardLeave?.(() => s.dirtyRef.current ? "Your Custom limit changes haven't been applied and are lost. The applied limits stay." : null);
    return () => guardLeave?.(null);
  }, []);
  const { ready, setupErr, initialize, enabled, preset, profilePending, profileError, game, perGame,
    acSeparate, editingAc, acOnline, onCpuBusyChange, setCpuNote } = s;

  // The error shows as soon as one read fails, also while others still run; initialize() ignores a click meanwhile.
  if (!ready) return statusPlaceholder({ label: "TDP status unavailable", error: setupErr, onRetry: () => void initialize() });

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
