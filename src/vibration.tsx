// SPDX-License-Identifier: BSD-3-Clause AND MIT
// Copyright (c) 2026 Rayekkk
// Portions copyright (c) 2026 piyush-tyagi-13 and M4ttiA, MIT - see LICENSE.MIT
// https://github.com/Rayekkk/LeGo-Vibe-Control

import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { addEventListener, removeEventListener, useQuickAccessVisible } from "@decky/api";
import { useState, useEffect, useCallback, useRef } from "react";
import { errorText, mutationQueue } from "./shared";
import { detailsRows, infoRow, statusPlaceholder } from "./ui";
import { betaText, driverNotice, FALLBACK_MODES, LEVELS_SHORT, modeShort, touchpadStrengthEditable } from "../desktop/native/vibration.mjs";
import {
  AppWatcher, checkResult, DEFAULT_APP, getCapabilities, getDriverStatus, getSettings, isReady, notify, notifyFailure,
  resetToDefault, setIntensity, setProfileOverwrite, setRumbleMode, setTouchpadEnabled, setTouchpadIntensity,
  testVibration, useDebouncedWrites, vibeReads, type ApplyResponse, type DriverStatus, type SettingsResponse, type VibeSettings,
} from "./vibration_api";

const LEVEL_NOTCHES = LEVELS_SHORT;

const styles = { container: { display: "flex", flexDirection: "column" as const, gap: "4px" } };

// ── Main component ────────────────────────────────────────────────────────────

const DEFAULT_SETTINGS: VibeSettings = {
  level: 2,
  mode: 0,
  touchpadIntensity: 2,
  touchpadEnabled: true,
};

const mutations = mutationQueue();

/** strengthOnly: the Legion Go S beta (strengthOnly(get_version.device)). */
const LGoVibeControl = ({ strengthOnly = false }: { strengthOnly?: boolean }) => {
  const [settings, setSettings] = useState<VibeSettings>(DEFAULT_SETTINGS);
  const [modes, setModes] = useState<string[]>(FALLBACK_MODES);
  const [driver, setDriver] = useState<DriverStatus | null>(null);
  const [appId, setAppId] = useState(DEFAULT_APP);
  const [gameName, setGameName] = useState("");
  const [perGameOn, setPerGameOn] = useState(false);
  // Go S: the backend sends nothing until its owner first changes a setting (reported as configured: false).
  const [unsent, setUnsent] = useState(false);
  const userChanged = useRef(false);

  const [loading, setLoading] = useState(true);
  const [setupErr, setSetupErr] = useState<string | null>(null);
  const [loadError, setLoadError] = useState("");
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [applying, setApplying] = useState(false);
  const [profileChanging, setProfileChanging] = useState(false);
  const [testing, setTesting] = useState(false);
  const [showDetails, setShowDetails] = useState(false);

  const visible = useQuickAccessVisible();
  const initialDriverReadRef = useRef(false);
  const initialLoadRef = useRef<Promise<void> | null>(null);
  const driverRevisionRef = useRef(0);
  const mounted = useRef(true);
  const currentApp = useRef(DEFAULT_APP);
  const gameRevision = useRef(0);
  const mutationsPending = useRef(0);
  const controlActionPending = useRef(false);
  const testPending = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);

  const { debounce, flushPendingWrites, pendingWrites } = useDebouncedWrites();

  // Bumped by every optimistic edit. A reply may only overwrite the UI while it
  // is still the newest thing that happened, otherwise a slow sysfs write snaps
  // the slider back to a value the user has already moved off.
  const editSeq = useRef(0);
  const latestFieldEdit = useRef<Record<string, number>>({});

  const adoptResponse = useCallback((res: SettingsResponse, readEpoch?: number) => {
    if (!mounted.current) return;
    const changedGame = currentApp.current !== res.app_id;
    if (!changedGame && readEpoch !== undefined && readEpoch !== vibeReads.epoch) return;
    // A heartbeat for the same game can carry a snapshot from before a queued
    // edit. Keep the draft until that edit answers; a new game always wins.
    if (!changedGame && (mutationsPending.current || Object.keys(pendingWrites.current).length)) return;
    if (changedGame) gameRevision.current += 1;
    currentApp.current = res.app_id;
    // Counts as an edit: switching game replaces the whole profile, and a field
    // reply still in flight from the previous one must not undo that.
    editSeq.current += 1;
    setSettings(res.settings);
    setAppId(res.app_id);
    setPerGameOn(res.overwrite);
    setUnsent(res.configured === false && !userChanged.current);
    setGameName(AppWatcher.displayName());
  }, []);

  const enqueueMutation = useCallback(<T,>(operation: () => Promise<T>) => {
    vibeReads.epoch += 1;
    mutationsPending.current += 1;
    if (mounted.current) setApplying(true);
    return mutations.push(operation).finally(() => {
      vibeReads.epoch += 1;
      mutationsPending.current -= 1;
      mutations.done();
      if (mounted.current) setApplying(mutationsPending.current > 0);
    });
  }, []);

  const refreshDriver = useCallback(async () => {
    const revision = driverRevisionRef.current;
    try {
      const status = await getDriverStatus();
      if (mounted.current && revision === driverRevisionRef.current) setDriver(status);
    } catch (e) {
    }
  }, []);

  // Initial load. Gated on is_ready so a backend that failed to start shows the
  // reason instead of sliders that silently do nothing.
  useEffect(() => {
    let active = true;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      if (initialLoadRef.current) await initialLoadRef.current;
      if (!active) return;
      const request = (async () => {
        try {
          const state = await isReady();
          if (!active) return;
          if (state.error) {
            setSetupErr(state.error);
            setLoading(false);
            return;
          }
          if (!state.ready) {
            if (active) retryTimer = setTimeout(check, 1000);
            return;
          }
          if (mutations.pending) await mutations.tail;
          if (!active) return;
          const driverRevision = driverRevisionRef.current;
          const settingsRevision = editSeq.current;
          const reads = [
            getSettings(),
            getDriverStatus(),
            getCapabilities().catch(() => ({ mode: FALLBACK_MODES })),
          ] as const;
          let snapshot: [SettingsResponse, DriverStatus, { mode: string[] }];
          try { snapshot = await Promise.all(reads); }
          catch (error) {
            if (active) {
              setLoadError(errorText(error, "Vibration settings could not be loaded. Check the Decky connection and retry."));
              setLoading(false);
            }
            // A rejection does not finish the other RPCs. Keep a retry behind
            // this attempt instead of filling the backend queue with duplicates.
            await Promise.allSettled(reads);
            throw error;
          }
          const [current, status, caps] = snapshot;
          if (!active) return;
          if (settingsRevision === editSeq.current) adoptResponse(current);
          if (driverRevision === driverRevisionRef.current) setDriver(status);
          initialDriverReadRef.current = true;
          setModes(caps?.mode?.length ? caps.mode : FALLBACK_MODES);
          setLoadError("");
          setLoading(false);
        } catch (e) {
          if (!active) return;
          setLoadError(errorText(e, "Vibration settings could not be loaded. Check the Decky connection and retry."));
          setLoading(false);
        }
      })();
      initialLoadRef.current = request;
      try { await request; }
      finally { if (initialLoadRef.current === request) initialLoadRef.current = null; }
    };
    void check();
    return () => { active = false; if (retryTimer !== undefined) clearTimeout(retryTimer); };
  }, [adoptResponse, loadAttempt]);

  // Hotplug pushes the driver status from the backend, so the dot is right even
  // while the panel is shut. Registered unconditionally: a controller is plugged
  // in with the Quick Access Menu closed far more often than with it open, and
  // adopting the state on arrival beats discovering it on the next open.
  //
  // Only the status. A hotplug re-applies the stored profile without changing
  // it, so a settings payload would carry nothing new - and feeding one to
  // adoptResponse bumps editSeq, which would make the panel throw away the
  // reply to an edit the user was making at that moment.
  useEffect(() => {
    const onDevice = (status: DriverStatus) => {
      driverRevisionRef.current += 1;
      setDriver(status);
    };
    addEventListener<[DriverStatus]>("device", onDevice);
    return () => removeEventListener<[DriverStatus]>("device", onDevice);
  }, []);

  // Still re-checked on open, as the backstop for the cases no event covers:
  // without pyudev there is no hotplug monitor at all, and a plugin reload
  // starts with whatever the hardware already is.
  useEffect(() => {
    if (loading || loadError || setupErr) return;
    const initialRead = initialDriverReadRef.current;
    initialDriverReadRef.current = false;
    if (visible && !initialRead) void refreshDriver();
  }, [visible, loading, loadError, setupErr, refreshDriver]);

  // Game changes are applied by the backend; just adopt what it reports.
  useEffect(() => AppWatcher.listen(adoptResponse), [adoptResponse]);

  // Handlers

  const applyField = useCallback(
    async (key: string, optimistic: Partial<VibeSettings>, call: (app: string, profile: string) => Promise<ApplyResponse>) => {
      if (!mounted.current || controlActionPending.current) return;
      userChanged.current = true;
      setUnsent(false);
      vibeReads.epoch += 1;
      const seq = ++editSeq.current;
      const revision = gameRevision.current;
      const expectedApp = appId;
      const expectedProfile = perGameOn ? appId : DEFAULT_APP;
      const fieldScope = JSON.stringify([expectedApp, expectedProfile, key]);
      latestFieldEdit.current[fieldScope] = seq;
      setSettings((prev) => ({ ...prev, ...optimistic }));
      debounce(key, () => {
        const isCurrent = () => mounted.current && revision === gameRevision.current && seq === editSeq.current;
        const request = enqueueMutation(async () => {
          // Debouncing alone cannot coalesce gestures already waiting behind a
          // slow write. Keep the latest accepted value for each field/profile,
          // including when closing the page flushes its final gesture.
          if (latestFieldEdit.current[fieldScope] !== seq) return;
          return call(expectedApp, expectedProfile);
        });
        request.then((res) => {
            if (!res || !isCurrent()) return;
            checkResult("Could not apply setting", res);
            if (res.app_id !== undefined && res.profile_id !== undefined && res.overwrite !== undefined)
              adoptResponse(res as SettingsResponse);
            else if (res.settings) setSettings(res.settings);
          })
          .catch((e) => {
            if (!isCurrent()) return;
            notifyFailure("Could not apply setting", e);
            // Only resync when nothing newer is pending; the newer edit's own
            // reply is the one that should decide what the panel shows.
            void getSettings().then(res => { if (isCurrent()) adoptResponse(res); }).catch(() => undefined);
          });
      });
    },
    [debounce, adoptResponse, enqueueMutation, appId, perGameOn],
  );

  const handleLevel = useCallback(
    (val: number) => void applyField("level", { level: val }, (app, profile) => setIntensity(val, app, profile)),
    [applyField],
  );

  const handleMode = useCallback(
    // No sample is played here on purpose: the controller firmware already
    // demonstrates the new pattern. Adding our own put a second rumble on
    // top of it, which is why the same mode felt different every time.
    (val: number) => void applyField("mode", { mode: val }, (app, profile) => setRumbleMode(val, app, profile)),
    [applyField],
  );

  const handleTpIntensity = useCallback(
    (val: number) =>
      void applyField("tpIntensity", { touchpadIntensity: val }, (app, profile) => setTouchpadIntensity(val, app, profile)),
    [applyField],
  );

  const handleTpToggle = useCallback(
    (val: boolean) =>
      void applyField("tpEnabled", { touchpadEnabled: val }, (app, profile) => setTouchpadEnabled(val, app, profile)),
    [applyField],
  );

  const handleReset = useCallback(async () => {
    if (!mounted.current || controlActionPending.current) return;
    controlActionPending.current = true;
    userChanged.current = true;
    setUnsent(false);
    setProfileChanging(true);
    const expectedApp = appId, expectedProfile = perGameOn ? appId : DEFAULT_APP;
    const revision = gameRevision.current;
    flushPendingWrites();
    const seq = ++editSeq.current;
    const isCurrent = () => mounted.current && revision === gameRevision.current && seq === editSeq.current;
    try {
      const res = await enqueueMutation(() => resetToDefault(expectedApp, expectedProfile));
      if (!isCurrent()) return;
      // Reset commits its profile before applying hardware. A failed apply
      // still carries the authoritative saved settings and profile context.
      if (res.app_id !== undefined && res.profile_id !== undefined && res.overwrite !== undefined)
        adoptResponse(res as SettingsResponse);
      else if (res.settings) setSettings(res.settings);
      if (checkResult("Reset failed", res)) {
        notify("Defaults restored.");
      }
    } catch (e) {
      if (isCurrent()) notifyFailure("Reset failed", e);
    } finally {
      controlActionPending.current = false;
      if (mounted.current) setProfileChanging(false);
    }
  }, [appId, perGameOn, enqueueMutation, flushPendingWrites, adoptResponse]);

  const handleTest = useCallback(async () => {
    if (!mounted.current || testPending.current || controlActionPending.current) return;
    testPending.current = true;
    setTesting(true);
    const revision = gameRevision.current;
    try {
      // A slider value is optimistic until its debounced save completes.
      // Test the confirmed value from this gesture, rather than the old one.
      flushPendingWrites();
      await mutations.tail;
      if (!mounted.current || revision !== gameRevision.current) return;
      await testVibration(500);
    } catch (e) {
      notifyFailure("Test vibration failed", e);
    } finally {
      testPending.current = false;
      if (mounted.current) setTesting(false);
    }
  }, [flushPendingWrites]);

  const handlePerGameToggle = useCallback(
    async (val: boolean) => {
      if (!mounted.current || controlActionPending.current) return;
      controlActionPending.current = true;
      userChanged.current = true;
      setUnsent(false);
      setProfileChanging(true);
      const revision = gameRevision.current;
      flushPendingWrites();
      ++editSeq.current;
      const isCurrent = () => mounted.current && revision === gameRevision.current;
      setPerGameOn(val);
      try {
        // The backend applies the resolved profile for us, which is what the
        // old frontend skipped when *enabling* a profile - the UI showed the
        // game's values while the hardware kept the global ones.
        const name = AppWatcher.displayName();
        const res = await enqueueMutation(() => setProfileOverwrite(appId, val, name));
        if (!isCurrent()) return;
        checkResult("Could not switch profile", res);
        if (res.app_id !== undefined && res.profile_id !== undefined && res.overwrite !== undefined)
          adoptResponse(res as SettingsResponse);
        else if (res.ok) setSettings(res.settings);
        else setPerGameOn(!val);
      } catch (e) {
        if (isCurrent()) {
          setPerGameOn(!val);
          notifyFailure("Could not switch profile", e);
          try {
            const snapshot = await getSettings();
            if (isCurrent()) adoptResponse(snapshot);
          } catch (readError) {
            if (isCurrent()) setLoadError(errorText(readError, "Could not confirm the active vibration profile."));
          }
        }
      } finally {
        controlActionPending.current = false;
        if (mounted.current) setProfileChanging(false);
      }
    },
    [appId, enqueueMutation, flushPendingWrites, adoptResponse],
  );

  // Render

  const retryLoad = () => {
    setLoadError(""); setSetupErr(null); setLoading(true);
    setLoadAttempt(attempt => attempt + 1);
  };

  if (loadError || setupErr || loading) return statusPlaceholder({
    label: loadError ? "Vibration settings unavailable" : "Vibration unavailable", error: loadError || setupErr, onRetry: retryLoad });

  const driverFound = driver?.found ?? false;
  const missingDriver = driverNotice(driver);
  const gameRunning = appId !== DEFAULT_APP;
  const driverLabel = strengthOnly ? "hid-lenovo-go-s" : "hid-lenovo-go";

  return (
    <div style={styles.container}>
      <PanelSection title="Vibration">
        {missingDriver && infoRow(missingDriver[0], missingDriver[1])}
        {strengthOnly && infoRow("Beta", betaText({ configured: !unsent }))}
        <PanelSectionRow>
          <ToggleField
            label="Per-game profile"
            description={!gameRunning ? "Start a game to give it its own settings."
              : perGameOn ? `${gameName} has its own settings.` : `${gameName} uses the global settings.`}
            checked={perGameOn && gameRunning}
            disabled={!gameRunning || applying}
            onChange={handlePerGameToggle}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Vibration strength"
            disabled={profileChanging}
            value={settings.level}
            min={0}
            max={3}
            step={1}
            notchCount={4}
            notchLabels={LEVEL_NOTCHES.map((label, notchIndex) => ({ notchIndex, label }))}
            onChange={handleLevel}
          />
        </PanelSectionRow>
        {!strengthOnly && <PanelSectionRow>
          <SliderField
            label="Vibration mode"
            description="A vibration pattern tuned for a game type."
            disabled={profileChanging}
            value={settings.mode}
            min={0}
            max={Math.max(0, modes.length - 1)}
            step={1}
            notchCount={modes.length}
            notchLabels={modes.map((raw, notchIndex) => ({ notchIndex, label: modeShort(raw) }))}
            onChange={handleMode}
          />
        </PanelSectionRow>}
      </PanelSection>

      <PanelSection title="Touchpad">
        <PanelSectionRow>
          <ToggleField
            label="Touchpad vibration"
            disabled={profileChanging}
            checked={settings.touchpadEnabled}
            onChange={handleTpToggle}
          />
        </PanelSectionRow>
        {!strengthOnly && <PanelSectionRow>
          <SliderField
            label="Touchpad strength"
            value={settings.touchpadIntensity}
            min={0}
            max={3}
            step={1}
            notchCount={4}
            notchLabels={LEVEL_NOTCHES.map((label, notchIndex) => ({ notchIndex, label }))}
            disabled={profileChanging || !touchpadStrengthEditable(settings)}
            onChange={handleTpIntensity}
          />
        </PanelSectionRow>}
      </PanelSection>

      <PanelSection title="More">
        {!strengthOnly && <PanelSectionRow>
          <ButtonItem
            layout="below"
            description="A short buzz with the current settings."
            onClick={handleTest}
            disabled={applying || testing}
          >
            {testing ? "Vibrating…" : "Test vibration"}
          </ButtonItem>
        </PanelSectionRow>}
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleReset} disabled={applying || testing}>
            Reset to defaults
          </ButtonItem>
        </PanelSectionRow>
        {detailsRows(showDetails, () => setShowDetails(value => !value), [
          ["Driver", driverFound
            ? `${driverLabel}${driver?.method ? ` · ${driver.method}` : ""}${driver?.ids ? ` · ${driver.ids}` : ""}`
            : `${driverLabel} not detected`],
          ["Endpoints", <span style={{ display: "block", maxWidth: "100%", overflowWrap: "anywhere" }}>
            {driver?.paths?.join(" · ") || "No endpoint reported"}
          </span>],
        ])}
      </PanelSection>
    </div>
  );
};

// ── Plugin entry point ────────────────────────────────────────────────────────

export const VibrationPage = LGoVibeControl;
export const getVibrationSettings = async () => {
  if (mutations.pending) await mutations.tail;
  return getSettings();
};
export const startVibrationWatcher = () => AppWatcher.start();
export const stopVibrationWatcher = () => AppWatcher.stop();
