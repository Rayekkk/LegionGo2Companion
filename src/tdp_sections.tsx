// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk
// https://github.com/Rayekkk/LeGoTDP
// TDP page: its sections.

import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { confirmAction, detailsRows, infoRow } from "./shared";
import {
  absolute, BATTERY_PRESETS_LABEL, BATTERY_PRESETS_NOTE, extrasConfirm, extrasNote, extrasShown, FALLBACK_MAX,
  fromAbsolute, helperRetryShown, limitsText, normalise, offsetMax, powerSummary, PRESET_LABELS, PRESET_ORDER,
  presetNote, resolvedPreset, scopeNote, simpleTuning, sourceNote, shownPresets, sourceText, tdpNotice,
  boostRoomNote, withFppt, withSpl, withSppt, type PresetKey, type Tuning,
} from "../desktop/native/tdp.mjs";
import {
  GameProfile, applyTdp, deleteGameProfile, getGameProfile, getSettings, retryExtras, savedTuning,
  setAdvancedTdpControlCall, setExtrasUnlockedCall, setGameAcProfile, setGameProfileActive, setPluginEnabled,
  setSeparateBatteryPresetsCall, toMw, toW,
} from "./tdp_api";
import { type Scope, type TdpState } from "./tdp_state";


export const noticeSection = (s: TdpState) => {
  const { capsNow, extrasUnlocked, profileError, profilePending, setProfilePending, setProfileError,
    setProfileAttempt } = s;
  const notice = tdpNotice(capsNow, extrasUnlocked);
  return (notice || profileError) && <PanelSection>
    {notice && infoRow(notice[0], notice[1])}
    {profileError && <>
      {infoRow("Could not load profile", profileError)}
      <PanelSectionRow><ButtonItem layout="below" onClick={() => {
        if (profilePending) return;
        setProfilePending(true); setProfileError("");
        setProfileAttempt(attempt => attempt + 1);
      }}>Retry profile</ButtonItem></PanelSectionRow>
    </>}
  </PanelSection>;
};

export const powerSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, dirtyRef,
    draftBaseRef, preset, setPreset, tuning, setTuning, acTuning, setAcTuning, active, setActive,
    advancedTdpControl, caps, minW, presets, editingAc, setEditingAc, perGame, setPerGame, game,
    acSeparate, setAcSeparate, acOnline, tdpAvailable, enabled, setEnabled, setSavedPreset, savedPreset,
    setSavedAcPreset, savedAcPreset, globalProfile, setGlobalProfile, adoptGlobalSettings, showGameProfile,
    setProfileAttempt, profilePending, profileError, info, extrasUnlocked, stdCaps, cpuBusy, loading,
    cpuBusyOwnersRef, separateBattery, batteryPresets, running: backendRunning } = s;
  /** Run `proceed` now, or after confirming that the unsaved Custom limits are dropped. */
  const askDiscard = (proceed: () => void) => {
    if (!dirtyRef.current) return proceed();
    confirmAction("Discard unsaved changes?", "Your changes to these limits haven't been applied.", "Discard", () => {
      const base = draftBaseRef.current;
      if (base) (base.ac ? setAcTuning : setTuning)(base.tuning);
      clearDraft();
      proceed();
    });
  };

  // ── Preset handler ────────────────────────────────────────────────────────────
  const handlePresetChange = async (key: PresetKey) => {
    const scope = beginOperation();
    if (!scope) return;
    const prevPreset = preset;
    const prevTuning = tuning, prevAcTuning = acTuning;
    // A preset replaces the Custom sliders, so their draft is dropped without asking.
    clearDraft();
    setPreset(key);
    if (key === "custom") {
      if (!advancedTdpControl) setActive(simpleTuning(active.spl, caps, minW));
      finishOperation(scope);
      return;
    }

    const vals = presets[key];
    const next = normalise(fromAbsolute(vals.spl, vals.sppt, vals.fppt), caps, minW);
    if (editingAc) setAcTuning(next); else setTuning(next);
    const appId = (perGame && game) ? game.appId : "";
    const a = absolute(next);
    try {
      if (editingAc && appId) {
        await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, key);
        if (!operationCurrent(scope)) return;
        setSavedAcPreset(key);
        done(`${PRESET_LABELS[key]} saved for ${game!.name} on charger.`);
      } else {
        await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, key,
          scope.appId);
        if (!operationCurrent(scope)) return;
        if (!appId) setGlobalProfile({ ...a, preset: key });
        else setSavedPreset(key);
        done(appId ? `${PRESET_LABELS[key]} saved for ${game!.name}.` : `${PRESET_LABELS[key]} applied.`);
      }
    } catch (e: unknown) {
      if (!operationCurrent(scope)) return;
      setPreset(prevPreset);
      if (editingAc) setAcTuning(prevAcTuning); else setTuning(prevTuning);
      showError("preset", e);
    } finally { finishOperation(scope); }
  };
  // ── Settings for {game} ───────────────────────────────────────────────────────
  const scope: Scope = !perGame ? "global" : acSeparate ? "game_ac" : "game";
  const applyScope = async (next: Scope) => {
    if (!game) return;
    const op = beginOperation(tdpAvailable);
    if (!op) return;
    const app = game;
    try {
      // Without a TDP provider the choice only selects which CPU settings to edit.
      if (!tdpAvailable) { setPerGame(next !== "global"); return; }
      if (next === "global") {
        await setGameProfileActive(app.appId, false);
        if (!operationCurrent(op)) return;
        setPerGame(false); setAcSeparate(false); setEditingAc(false);
        setSavedPreset(undefined); setSavedAcPreset(undefined);
        const s = await getSettings();
        if (!operationCurrent(op)) return;
        adoptGlobalSettings(s);
        done(`${app.name} uses Global settings.`);
        return;
      }
      let profile: GameProfile | null = null;
      if (!perGame) {
        await setGameProfileActive(app.appId, true);
        if (!operationCurrent(op)) return;
        profile = await getGameProfile(app.appId);
        if (!operationCurrent(op)) return;
        if (!profile.exists || !profile.profile) throw new Error("Game profile data is missing or corrupt.");
        showGameProfile(profile);
      }
      const separate = profile ? profile.ac_separate : acSeparate;
      if ((next === "game_ac") !== separate) {
        // Hand the stored charger values back unchanged, so splitting again restores them.
        const stored = profile ?? await getGameProfile(app.appId);
        if (!operationCurrent(op)) return;
        const ac = stored.ac_profile;
        if (!stored.exists || !ac) throw new Error("Game profile data is missing or corrupt.");
        await setGameAcProfile(app.appId, ac.spl, ac.sppt, ac.fppt, next === "game_ac", "");
        if (!operationCurrent(op)) return;
        setAcSeparate(next === "game_ac");
        setAcTuning(savedTuning(ac, ac.ac_preset, presets, advancedTdpControl, caps, minW).tuning);
        setEditingAc(next === "game_ac" && acOnline);
        setSavedAcPreset(next === "game_ac" ? (ac.ac_preset ?? "") : undefined);
      }
      done(next === "game_ac" ? `${app.name} has separate battery and charger settings.`
        : `Only ${app.name} uses these settings.`);
    } catch (e: unknown) {
      if (!operationCurrent(op)) return;
      showError("scope", e);
      // A step may have landed before the failure; show what the backend kept.
      setProfileAttempt(attempt => attempt + 1);
    } finally { finishOperation(op); }
  };

  // ── Enable / disable plugin ───────────────────────────────────────────────────
  const handleEnabledToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
      await setPluginEnabled(checked);
      if (!operationCurrent(op)) return;
      setEnabled(checked);
      done(checked ? "TDP control on." : "TDP control off. Lenovo's limits are back.");
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("enabled", e);
    } finally { finishOperation(op); }
  };

  // What runs now: the game's charger values on AC when it has them, else its battery or the global values.
  const running = perGame && acSeparate && acOnline ? acTuning : tuning;
  const runningStored = !perGame ? globalProfile.preset : acSeparate && acOnline ? savedAcPreset : savedPreset;
  const stored = absolute(running);
  const nowKey = resolvedPreset(stored.spl, stored.sppt, stored.fppt, runningStored, presets);
  // The backend names what runs now; without it (older backend) the page's own scope and values stand in.
  const nowLabel = backendRunning ? PRESET_LABELS[backendRunning.preset] : PRESET_LABELS[nowKey];
  // A preset runs its battery values on battery with Separate battery presets; Custom runs as saved.
  const now = backendRunning ?? (nowKey === "custom" ? stored : shownPresets(presets, batteryPresets, separateBattery, !acOnline)[nowKey]);
  const runningOwner = backendRunning ? backendRunning.game : perGame;
  const enableDescription = failureFor("enabled") ?? (enabled && (profilePending || profileError) ? "Reading profile…"
    : powerSummary(enabled, runningOwner && game ? game.name : "Global", nowLabel, now,
      info?.values?.package_draw, extrasUnlocked, stdCaps));
  const presetDescription = failureFor("preset") ?? presetNote(preset, shownPresets(presets, batteryPresets,
    separateBattery, !editingAc && ((perGame && acSeparate) || !acOnline)));
  const scopeDescription = !game ? "" : failureFor("scope") ?? scopeNote(scope === "game_ac" ? "charger" : scope, game.name);
  const sourceDescription = sourceNote(acOnline, editingAc);

  return (
    <PanelSection title="Power">
      <PanelSectionRow>
        <ToggleField
          label="Enable TDP control"
          description={enableDescription}
          checked={enabled}
          disabled={cpuBusy || loading || !tdpAvailable}
          onChange={handleEnabledToggle}
        />
      </PanelSectionRow>
      {enabled && game && <PanelSectionRow>
        <DropdownItem
          label={`Settings for ${game.name}`}
          description={scopeDescription}
          selectedOption={scope}
          rgOptions={[{ data: "global", label: "Global" }, { data: "game", label: "This game" },
            { data: "game_ac", label: "This game + charger" }]}
          disabled={cpuBusy || loading}
          onChange={option => { if (option.data !== scope && !cpuBusyOwnersRef.current.size) askDiscard(() => void applyScope(option.data)); }}
        />
      </PanelSectionRow>}
      {enabled && game && perGame && acSeparate && <PanelSectionRow>
        <DropdownItem
          label="Edit settings for"
          description={sourceDescription}
          selectedOption={editingAc ? "charger" : "battery"}
          rgOptions={[{ data: "battery", label: "Battery" }, { data: "charger", label: "Charger" }]}
          disabled={cpuBusy || loading}
          onChange={option => {
            if ((option.data === "charger") === editingAc || cpuBusyOwnersRef.current.size) return;
            askDiscard(() => { if (!cpuBusyOwnersRef.current.size) setEditingAc(option.data === "charger"); });
          }}
        />
      </PanelSectionRow>}
      {enabled && PRESET_ORDER.map(key => (
        <PanelSectionRow key={key}>
          <ButtonItem
            layout="below"
            description={preset === key ? presetDescription : undefined}
            disabled={preset === key || loading || !tdpAvailable}
            onClick={() => void handlePresetChange(key)}
          >
            {preset === key ? `> ${PRESET_LABELS[key]}` : PRESET_LABELS[key]}
          </ButtonItem>
        </PanelSectionRow>
      ))}
    </PanelSection>
  );
};

export const customLimitsSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, dirty,
    setDirty, dirtyRef, draftBaseRef, active, setActive, caps, minW, advancedTdpControl, loading,
    tdpAvailable, operationLoading, editingAc, perGame, game, acSeparate, setTuning, setAcTuning, setSavedPreset,
    setSavedAcPreset, setGlobalProfile } = s;
  const om      = offsetMax(active.spl, caps);
  const editActive = (next: Tuning) => {
    if (!dirtyRef.current) draftBaseRef.current = { ac: editingAc, tuning: active };
    dirtyRef.current = true;
    setDirty(true);
    setActive(next);
  };
  const handlers = {
    onSpl: (v: number) => editActive(withSpl(active, v, caps, minW)),
    onSppt: (v: number) => editActive(withSppt(active, v, caps)),
    onFppt: (v: number) => editActive(withFppt(active, v, caps)),
  };

  // ── Apply (Custom mode only) ──────────────────────────────────────────────────
  const apply = async () => {
    const op = beginOperation();
    if (!op) return;
    const appId = (perGame && game) ? game.appId : "";
    const target = advancedTdpControl ? active : simpleTuning(active.spl, caps, minW);
    const a = absolute(target);
    try {
      if (editingAc && appId) {
        await setGameAcProfile(appId, toMw(a.spl), toMw(a.sppt), toMw(a.fppt), acSeparate, "custom");
        if (!operationCurrent(op)) return;
        setAcTuning(target);
        setSavedAcPreset("custom");
        clearDraft();
        done(`Saved for ${game!.name} on charger.`);
      } else {
        await applyTdp(
          toMw(a.spl), toMw(a.sppt), toMw(a.fppt), appId, "custom",
          op.appId);
        if (!operationCurrent(op)) return;
        setTuning(target);
        if (!appId) setGlobalProfile({ ...a, preset: "custom" });
        else setSavedPreset("custom");
        clearDraft();
        done(appId ? `Saved for ${game!.name}.` : "Custom limits applied.");
      }
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("apply", e);
    } finally { finishOperation(op); }
  };

  return (
    <PanelSection title={`Custom limits${dirty ? " · unsaved" : ""}`}>
      <PanelSectionRow>
        <SliderField
          label={`Sustained power · ${active.spl} W`}
          value={active.spl} min={minW} max={caps.spl} step={1}
          disabled={loading || !tdpAvailable}
          onChange={advancedTdpControl ? handlers.onSpl
            : (value) => editActive(simpleTuning(value, caps, minW))}
          description={advancedTdpControl ? undefined : "Boost follows automatically."}
        />
      </PanelSectionRow>
      {advancedTdpControl && <>
      <PanelSectionRow>
        <SliderField
          label={`Slow boost · +${active.spptOff} W`}
          value={active.spptOff} min={0} max={om.sppt || 1} step={1}
          disabled={om.sppt === 0 || loading || !tdpAvailable}
          onChange={handlers.onSppt}
          description={boostRoomNote(active.spl, active.spptOff, om.sppt)}
        />
      </PanelSectionRow>
      <PanelSectionRow>
        <SliderField
          label={`Fast boost · +${active.fpptOff} W`}
          value={active.fpptOff} min={0} max={om.fppt || 1} step={1}
          disabled={om.fppt === 0 || loading || !tdpAvailable}
          onChange={handlers.onFppt}
          description={boostRoomNote(active.spl, active.fpptOff, om.fppt)}
        />
      </PanelSectionRow>
      </>}
      <PanelSectionRow>
        <ButtonItem layout="below" onClick={apply} disabled={loading || !tdpAvailable}
          description={failureFor("apply") ?? (dirty ? "Unsaved changes" : undefined)}>
          {operationLoading ? "Applying…"
            : editingAc && game ? `Apply for ${game.name} on charger`
            : perGame && game ? `Apply for ${game.name}`
            : "Apply"}
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
};

export const moreSection = (s: TdpState) => {
  const { beginOperation, operationCurrent, finishOperation, done, showError, failureFor, clearDraft, setFailure,
    readCaps, adoptCaps, capsNow, advancedTdpControl, setAdvancedTdpControl, extrasUnlocked, setExtrasUnlocked,
    caps, stdCaps, minW, presets, tuning, setTuning, acTuning, setAcTuning, setPreset, perGame,
    setPerGame, setProfileExists, profileExists, setAcSeparate, acSeparate, setEditingAc, game, enabled,
    savedPreset, setSavedPreset, savedAcPreset, setSavedAcPreset, globalProfile, setGlobalProfile,
    adoptGlobalSettings, loading, tdpAvailable, operationLoading, details, setDetails, info, cpuNote,
    separateBattery, setSeparateBattery } = s;
  const deleteProfile = async () => {
    if (!game) return;
    const op = beginOperation();
    if (!op) return;
    const app = game;
    try {
      await deleteGameProfile(app.appId);
      if (!operationCurrent(op)) return;
      setPerGame(false); setProfileExists(false); setAcSeparate(false); setEditingAc(false);
      setSavedPreset(undefined); setSavedAcPreset(undefined);
      const s = await getSettings();
      if (!operationCurrent(op)) return;
      adoptGlobalSettings(s);
      done(`Profile for ${app.name} deleted.`);
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("delete", e);
    } finally { finishOperation(op); }
  };

  // ── Extras: unlock extended TDP range ────────────────────────────────────────
  const handleExtrasUnlockedToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
    try {
      await setExtrasUnlockedCall(checked);
      if (!operationCurrent(op)) return;
      setExtrasUnlocked(checked);
      setFailure(null);
      // Enabling may have just downloaded the helper, which widens the caps.
      if (checked) { const c = await readCaps(); if (operationCurrent(op)) adoptCaps(c); }
    } catch (e: unknown) {
      if (operationCurrent(op)) showError("extras", e);
      return;
    }
    if (checked) return;

    // The backend clamps every persisted target and the active hardware change
    // in one transaction. Mirror that result locally without issuing a second
    // apply that could race a game or charger transition.
    const battery = absolute(tuning), charging = absolute(acTuning);
    const batteryCustom = resolvedPreset(battery.spl, battery.sppt, battery.fppt,
      perGame ? savedPreset : globalProfile.preset, presets) === "custom";
    const acCustom = resolvedPreset(charging.spl, charging.sppt, charging.fppt,
      savedAcPreset, presets) === "custom";
    const t = !advancedTdpControl && batteryCustom
      ? simpleTuning(tuning.spl, stdCaps, minW)
      : normalise(tuning, stdCaps, minW);
    const at = !advancedTdpControl && acCustom
      ? simpleTuning(acTuning.spl, stdCaps, minW)
      : normalise(acTuning, stdCaps, minW);
    const tChanged = t.spl !== tuning.spl || t.spptOff !== tuning.spptOff ||
      t.fpptOff !== tuning.fpptOff;
    const atChanged = at.spl !== acTuning.spl || at.spptOff !== acTuning.spptOff ||
      at.fpptOff !== acTuning.fpptOff;
    setTuning(t);
    setAcTuning(at);
    setGlobalProfile((current) => {
      const clamped = normalise(
        fromAbsolute(current.spl, current.sppt, current.fppt), stdCaps, minW);
      const values = absolute(clamped);
      return values.spl !== current.spl || values.sppt !== current.sppt || values.fppt !== current.fppt
        ? { ...values, preset: "custom" }
        : current;
    });
    if (tChanged) {
      setPreset("custom");
      if (perGame) setSavedPreset("custom");
    }
    if (acSeparate && atChanged) setSavedAcPreset("custom");
    } finally { finishOperation(op); }
  };

  const handleAdvancedTdpControlToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
    try {
      await setAdvancedTdpControlCall(checked);
      if (!operationCurrent(op)) return;
      setAdvancedTdpControl(checked);
      setFailure(null);
      clearDraft();
    } catch (error) {
      if (operationCurrent(op)) showError("advanced", error);
      return;
    }
    if (!checked) {
      const battery = absolute(tuning), charging = absolute(acTuning);
      if (resolvedPreset(battery.spl, battery.sppt, battery.fppt,
          perGame ? savedPreset : globalProfile.preset, presets) === "custom")
        setTuning(simpleTuning(tuning.spl, caps, minW));
      if (resolvedPreset(charging.spl, charging.sppt, charging.fppt,
          savedAcPreset, presets) === "custom")
        setAcTuning(simpleTuning(acTuning.spl, caps, minW));
      setGlobalProfile((current) => {
        if (resolvedPreset(current.spl, current.sppt, current.fppt, current.preset, presets) !== "custom")
          return current;
        return { ...absolute(simpleTuning(current.spl,
          caps, minW)), preset: current.preset };
      });
    } else {
      try {
        const settings = await getSettings();
        if (!operationCurrent(op)) return;
        const global = fromAbsolute(toW(settings.spl), toW(settings.sppt), toW(settings.fppt));
        setGlobalProfile({ ...absolute(global), preset: settings.active_preset });
        if (perGame && game) {
          const appId = game.appId;
          const profile = await getGameProfile(appId);
          if (!operationCurrent(op)) return;
          if (profile.exists) {
            const p = profile.profile;
            const ac = profile.ac_profile;
            setTuning(fromAbsolute(toW(p.spl), toW(p.sppt), toW(p.fppt)));
            setAcTuning(fromAbsolute(toW(ac.spl), toW(ac.sppt), toW(ac.fppt)));
          }
        } else {
          setTuning(global);
        }
      } catch (error) {
        if (operationCurrent(op)) showError("advanced", error);
      }
    }
    } finally { finishOperation(op); }
  };

  const handleSeparateBatteryToggle = async (checked: boolean) => {
    const op = beginOperation();
    if (!op) return;
    try {
      await setSeparateBatteryPresetsCall(checked);
      if (!operationCurrent(op)) return;
      setSeparateBattery(checked);
      setFailure(null);
    } catch (error) {
      if (operationCurrent(op)) showError("battery", error);
    } finally { finishOperation(op); }
  };

  const handleHelperRetry = async () => {
    const op = beginOperation(false, false);
    if (!op) return;
    try {
      await retryExtras();
      if (!operationCurrent(op)) return;
      const c = await readCaps();
      if (operationCurrent(op)) { adoptCaps(c); setFailure(null); }
    } catch (error) {
      if (operationCurrent(op)) showError("helper", error);
    } finally { finishOperation(op); }
  };

  return (
    <PanelSection title="More">
      <PanelSectionRow>
        <ToggleField
          label="Separate boost limits"
          description={failureFor("advanced") ?? "Set slow and fast boost yourself in Custom."}
          checked={advancedTdpControl}
          disabled={loading || !tdpAvailable}
          onChange={handleAdvancedTdpControlToggle}
        />
      </PanelSectionRow>
      {extrasShown(capsNow) && <PanelSectionRow>
        <ToggleField
          label={`Allow up to ${FALLBACK_MAX.fppt} W`}
          description={failureFor("extras") ?? extrasNote(extrasUnlocked, capsNow, stdCaps)}
          checked={extrasUnlocked}
          disabled={loading || !tdpAvailable}
          onChange={checked => checked ? confirmAction(extrasConfirm(capsNow).title, extrasConfirm(capsNow).text,
            extrasConfirm(capsNow).ok, () => void handleExtrasUnlockedToggle(true))
            : void handleExtrasUnlockedToggle(false)}
        />
      </PanelSectionRow>}
      {tdpAvailable && <PanelSectionRow>
        <ToggleField
          label={BATTERY_PRESETS_LABEL}
          description={failureFor("battery") ?? BATTERY_PRESETS_NOTE}
          checked={separateBattery}
          disabled={loading}
          onChange={checked => void handleSeparateBatteryToggle(checked)}
        />
      </PanelSectionRow>}
      {enabled && game && profileExists && <PanelSectionRow>
        <ButtonItem layout="below" disabled={loading} description={failureFor("delete")}
          onClick={() => confirmAction(`Delete profile for ${game.name}?`,
            `Its power, CPU Boost and energy settings are removed. ${game.name} will use Global settings.`,
            "Delete", () => void deleteProfile())}>
          {`Delete profile for ${game.name}`}
        </ButtonItem>
      </PanelSectionRow>}
      {helperRetryShown(capsNow, extrasUnlocked) && <PanelSectionRow>
        <ButtonItem layout="below" disabled={operationLoading} description={failureFor("helper")}
          onClick={handleHelperRetry}>Retry helper download</ButtonItem>
      </PanelSectionRow>}
      {detailsRows(details, () => setDetails(value => !value), [
        ["Limits now", limitsText(info)],
        ["Set by", sourceText(info)],
        ...(cpuNote ? [["CPU energy", cpuNote] as [string, string]] : []),
      ])}
    </PanelSection>
  );
};

