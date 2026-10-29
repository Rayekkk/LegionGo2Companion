// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP
// TDP page: CPU Boost and energy preference.

import { useQuickAccessVisible } from "@decky/api";
import { ButtonItem, PanelSection, PanelSectionRow, SliderField, Spinner, ToggleField } from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { errorText } from "./shared";
import {
  boostNote, boostUsable, editorControls, EPP_NAMED_VALUES, EPP_PREFERENCE, eppLabel, eppNote, eppPercent, eppTitle,
  eppUnavailable, eppValue, type CpuPowerControls,
} from "../desktop/native/tdp.mjs";
import { getCpuPowerControls, notify, notifyFailure, setCpuBoost, setEpp } from "./tdp_api";

// ── CPU power controls ────────────────────────────────────────────────────────

export const EPP_DEBOUNCE_MS = 350;

export const unavailableCpuPowerControls = (error = ""): CpuPowerControls => ({
  success: false,
  available: false,
  cpu_boost: { available: false, enabled: null, error: "" },
  epp: {
    available: false,
    value: null,
    profiles: [],
    numeric_supported: false,
    numeric_value: null,
    min: 0,
    max: 255,
    error: "",
  },
  error,
});

/**
 * The backend ships with this page and canonicalises EPP values itself; fail
 * closed only on a reply without the structure the editor reads, or one for a
 * different profile than the editor asked for.
 */
export function checkedCpuPowerControls(value: unknown, appId: string, acProfile: boolean): CpuPowerControls {
  const raw = value as CpuPowerControls | null;
  if (!raw || typeof raw !== "object" || typeof raw.success !== "boolean" || typeof raw.available !== "boolean" ||
      !raw.cpu_boost || typeof raw.cpu_boost !== "object" || !raw.epp || typeof raw.epp !== "object" ||
      !Array.isArray(raw.epp.profiles)) {
    throw new Error("Backend returned incompatible CPU power controls.");
  }
  if (raw.profile && (raw.profile.app_id !== appId || raw.profile.ac_profile !== acProfile)) {
    throw new Error("Backend returned CPU controls for a different profile.");
  }
  return raw;
}

export type CpuPowerAction = "boost" | "epp";

export interface CpuPowerControlsSectionProps {
  appId?: string;
  acProfile?: boolean;
  expectedAppId?: string;
  powerSource?: boolean;
  onBusyChange?: (owner: object, busy: boolean) => void;
  onCompatibilityNote?: (note: string) => void;
}

export const CpuPowerControlsSection: FC<CpuPowerControlsSectionProps> = ({
  appId = "", acProfile = false, expectedAppId = "",
  powerSource = false, onBusyChange, onCompatibilityNote,
}: CpuPowerControlsSectionProps = {}) => {
  const [controls, setControls] = useState<CpuPowerControls | null>(null);
  const [loading, setLoading] = useState(false);
  const [changing, setChanging] = useState<CpuPowerAction | null>(null);
  const [eppPending, setEppPending] = useState(false);
  const [requestError, setRequestError] = useState("");
  const [failedAction, setFailedAction] = useState<CpuPowerAction | null>(null);
  const [eppDraft, setEppDraft] = useState(50);
  const visible = useQuickAccessVisible();

  const mountedRef = useRef(true);
  const visibleRef = useRef(visible);
  const controlsRef = useRef<CpuPowerControls | null>(null);
  const actionRef = useRef<CpuPowerAction | null>(null);
  const actionSequenceRef = useRef(0);
  const mutationEpochRef = useRef(0);
  const readSequenceRef = useRef(0);
  const readInFlightRef = useRef(false);
  const refreshAfterReadRef = useRef(false);
  const refreshControlsRef = useRef<(() => Promise<void>) | null>(null);
  const refreshAfterActionRef = useRef(false);
  const eppPendingRef = useRef(false);
  const flushEppRef = useRef<(() => void) | null>(null);
  const eppTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const busyOwnerRef = useRef({});
  const reportBusy = useCallback((busy: boolean) => {
    onBusyChange?.(busyOwnerRef.current, busy);
  }, [onBusyChange]);
  visibleRef.current = visible;

  const acceptControls = useCallback((next: CpuPowerControls) => {
    controlsRef.current = next;
    setControls(next);
    if (!eppPendingRef.current) setEppDraft(eppPercent(editorControls(next).epp, 10));
  }, []);

  const rollbackEppDraft = useCallback(() => {
    const confirmed = controlsRef.current;
    if (confirmed) setEppDraft(eppPercent(editorControls(confirmed).epp, 10));
  }, []);

  const cancelEppDebounce = useCallback(() => {
    if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
    eppTimerRef.current = null;
    flushEppRef.current = null;
    if (!eppPendingRef.current) return;
    eppPendingRef.current = false;
    setEppPending(false);
    if (!actionRef.current) reportBusy(false);
    rollbackEppDraft();
  }, [rollbackEppDraft, reportBusy]);

  const refreshControls = useCallback(async () => {
    if (!mountedRef.current || !visibleRef.current) return;
    const readId = ++readSequenceRef.current;
    // Visibility changes may request a refresh while a mutation is running.
    // Queue it; a getter must never supersede the authoritative setter reply.
    if (actionRef.current) {
      refreshAfterActionRef.current = true;
      return;
    }
    // Reopening or changing scope invalidates the older reply, but needs only
    // one fresh read after it finishes. Never queue overlapping hardware reads.
    if (readInFlightRef.current) {
      refreshAfterReadRef.current = true;
      setLoading(true);
      return;
    }
    readInFlightRef.current = true;
    refreshAfterReadRef.current = false;
    const mutationEpoch = mutationEpochRef.current;
    setLoading(true);
    try {
      const next = checkedCpuPowerControls(await getCpuPowerControls(appId, acProfile), appId, acProfile);
      if (!mountedRef.current || !visibleRef.current ||
          readId !== readSequenceRef.current || mutationEpoch !== mutationEpochRef.current ||
          actionRef.current) {
        if (actionRef.current) refreshAfterActionRef.current = true;
        return;
      }
      acceptControls(next);
      setRequestError(next.success ? "" : next.error);
    } catch (error) {
      if (!mountedRef.current || !visibleRef.current ||
          readId !== readSequenceRef.current || mutationEpoch !== mutationEpochRef.current ||
          actionRef.current) return;
      console.warn("[legiongo2companion] tdp.cpu_controls_unavailable");
      const message = "CPU power controls are unavailable in this backend build.";
      acceptControls(unavailableCpuPowerControls(message));
      setRequestError(message);
    } finally {
      readInFlightRef.current = false;
      if (mountedRef.current && readId === readSequenceRef.current &&
          mutationEpoch === mutationEpochRef.current && !actionRef.current) {
        setLoading(false);
      }
      if (refreshAfterReadRef.current) {
        refreshAfterReadRef.current = false;
        if (mountedRef.current && visibleRef.current) void refreshControlsRef.current?.();
      }
    }
  }, [acceptControls, appId, acProfile]);
  refreshControlsRef.current = refreshControls;

  useEffect(() => {
    if (!visible) {
      flushEppRef.current?.();
      ++readSequenceRef.current;
      setLoading(false);
      return;
    }
    if (actionRef.current) {
      refreshAfterActionRef.current = true;
      return;
    }
    void refreshControls();
  }, [visible, powerSource, cancelEppDebounce, refreshControls]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      flushEppRef.current?.();
      mountedRef.current = false;
      visibleRef.current = false;
      ++readSequenceRef.current;
      ++actionSequenceRef.current;
      ++mutationEpochRef.current;
      if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
      eppTimerRef.current = null;
      eppPendingRef.current = false;
      if (!actionRef.current) reportBusy(false);
    };
  }, []);

  const runAction = async (
    kind: CpuPowerAction,
    request: () => Promise<CpuPowerControls>,
  ) => {
    if (!mountedRef.current || actionRef.current) return;
    const actionId = ++actionSequenceRef.current;
    ++mutationEpochRef.current;
    ++readSequenceRef.current;
    actionRef.current = kind;
    reportBusy(true);
    setLoading(false);
    setChanging(kind);
    setRequestError("");
    setFailedAction(null);
    let refreshNeeded = false;

    try {
      const next = checkedCpuPowerControls(await request(), appId, acProfile);
      if (!mountedRef.current) {
        if (!next.success) notify("TDP", next.error || next.epp.error || next.cpu_boost.error);
        return;
      }
      if (actionId !== actionSequenceRef.current) {
        refreshNeeded = true;
        return;
      }
      acceptControls(next);
      if (!next.success) {
        const controlError = kind === "boost" ? next.cpu_boost.error : next.epp.error;
        const message = next.error || controlError || "The backend rejected the change.";
        setRequestError(message);
        setFailedAction(kind);
        notify("TDP", message);
      }
    } catch (error) {
      if (!mountedRef.current) {
        notifyFailure("TDP", error);
        return;
      }
      if (actionId !== actionSequenceRef.current) {
        refreshNeeded = true;
        return;
      }
      refreshNeeded = true;
      if (kind === "epp") rollbackEppDraft();
      setRequestError(errorText(error));
      setFailedAction(kind);
      notifyFailure("TDP", error);
    } finally {
      reportBusy(false);
      const current = actionId === actionSequenceRef.current;
      if (current) {
        actionRef.current = null;
        if (mountedRef.current) setChanging(null);
      }
      if (!mountedRef.current) return;

      const shouldRefresh = refreshNeeded || refreshAfterActionRef.current || !current;
      if (current) refreshAfterActionRef.current = false;
      if (shouldRefresh) {
        if (actionRef.current) refreshAfterActionRef.current = true;
        else if (visibleRef.current) void refreshControls();
      }
    }
  };

  const changeBoost = (enabled: boolean) => {
    const boost = controlsRef.current && editorControls(controlsRef.current).cpu_boost;
    if (!visibleRef.current || !boost?.available || boost.enabled == null ||
        actionRef.current || eppPendingRef.current || enabled === boost.enabled) return;
    void runAction("boost", () => setCpuBoost(enabled, appId, acProfile, expectedAppId));
  };

  const changeEpp = (value: string) => {
    const current = controlsRef.current && editorControls(controlsRef.current);
    const epp = current?.epp;
    if (!visibleRef.current || !epp?.available || actionRef.current ||
        (value === epp.value && (current?.profile?.epp == null || value === current.profile.epp))) return;
    void runAction("epp", () => setEpp(value, appId, acProfile, expectedAppId));
  };

  const scheduleNumericEpp = (value: number) => {
    const epp = controlsRef.current && editorControls(controlsRef.current).epp;
    if (!mountedRef.current || !visibleRef.current || !epp?.available ||
        !epp.numeric_supported || actionRef.current) return;
    const percent = Math.round(Math.min(Math.max(value, 0), 100) / 10) * 10;
    const rawValue = eppValue(percent);
    setEppDraft(percent);
    eppPendingRef.current = true;
    setEppPending(true);
    reportBusy(true);
    if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
    const flush = () => {
      // clearTimeout cannot retract a callback already queued by the browser.
      // Only the current, unflushed gesture may clear the timer or write EPP.
      if (flushEppRef.current !== flush) return;
      if (eppTimerRef.current) clearTimeout(eppTimerRef.current);
      eppTimerRef.current = null;
      flushEppRef.current = null;
      eppPendingRef.current = false;
      setEppPending(false);
      // Keep the scope and foreground game from this gesture, including when
      // a remount flushes the slider after switching game or battery/AC editor.
      void runAction("epp", () => setEpp(rawValue, appId, acProfile, expectedAppId));
    };
    flushEppRef.current = flush;
    eppTimerRef.current = setTimeout(flush, EPP_DEBOUNCE_MS);
  };

  // The compatibility note is a technical fact; the page shows it under Show details.
  useEffect(() => { onCompatibilityNote?.(controls?.epp.compatibility_note ?? ""); },
    [controls?.epp.compatibility_note, onCompatibilityNote]);

  if (!controls) return (
    <PanelSection title="CPU">
      <PanelSectionRow><Spinner /></PanelSectionRow>
    </PanelSection>
  );

  const shown = editorControls(controls);
  const boost = shown.cpu_boost;
  const epp = shown.epp;
  const busy = loading || changing !== null || eppPending;
  const title = eppTitle(epp);
  const biasProfiles = epp.profiles.filter((profile) =>
    Object.prototype.hasOwnProperty.call(EPP_NAMED_VALUES, profile));
  const specialProfiles = epp.profiles.filter((profile) =>
    !Object.prototype.hasOwnProperty.call(EPP_NAMED_VALUES, profile));
  const profileIndex = epp.value ? biasProfiles.indexOf(epp.value) : -1;
  const profileOffset = profileIndex >= 0 ? 0 : 1;
  const discreteProfileValue = profileIndex >= 0 ? profileIndex : 0;
  const noDiscreteChoice = profileIndex >= 0
    ? biasProfiles.length < 2
    : biasProfiles.length < 1;
  // A failed change shows on its control; a failed read on CPU Boost, the first row.
  const globalError = requestError || controls.error;
  const errorFor = (kind: CpuPowerAction) =>
    (failedAction ?? "boost") === kind ? globalError : "";

  return (
    <PanelSection title="CPU">
      <PanelSectionRow>
        <ToggleField
          label="CPU Boost"
          description={errorFor("boost") || (
            boostUsable(boost) && changing === "boost" ? "Applying…" : boostNote(boost))}
          checked={boost.enabled === true}
          disabled={busy || !boostUsable(boost)}
          onChange={changeBoost}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        {epp.numeric_supported ? (
          <SliderField
            label={title}
            value={eppDraft}
            min={0}
            max={100}
            step={10}
            showValue={true}
            valueSuffix="%"
            disabled={loading || changing !== null || !epp.available}
            onChange={scheduleNumericEpp}
            description={errorFor("epp") || (
              epp.available && (changing === "epp" || eppPending) ? "Applying…" : eppNote(epp))}
          />
        ) : (
          <SliderField
            label={title}
            value={discreteProfileValue}
            min={0}
            max={Math.max(1, biasProfiles.length - 1 + profileOffset)}
            step={1}
            showValue={false}
            disabled={busy || !epp.available || noDiscreteChoice}
            onChange={(index) => {
              const profile = biasProfiles[Math.round(index) - profileOffset];
              if (profile) changeEpp(profile);
            }}
            description={errorFor("epp") || (
              !epp.available ? eppUnavailable(epp)
              : changing === "epp" ? "Applying…"
              : noDiscreteChoice ? "No other choice on this device."
              : profileIndex < 0 ? `Choose: ${biasProfiles.map(eppLabel).join(" / ")}`
              : EPP_PREFERENCE)}
          />
        )}
      </PanelSectionRow>
      {!epp.numeric_supported && specialProfiles.map((profile) => (
        <PanelSectionRow key={`epp-${profile}`}>
          <ButtonItem
            layout="below"
            disabled={busy || !epp.available || (controls.profile?.epp ?? epp.value) === profile}
            onClick={() => changeEpp(profile)}
          >
            {`Use ${eppLabel(profile)}`}
          </ButtonItem>
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
};

