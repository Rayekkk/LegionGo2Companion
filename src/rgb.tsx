// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable, toaster, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  DropdownItem,
  DropdownOption,
  gamepadSliderClasses,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import type { Result } from "./result";
import { infoRow, mutationQueue, statusPlaceholder, rpc } from "./shared";
import { controlNote, effectsEditable, POWER_LED_BETA_TEXT, powerChecked as powerShown, powerNote, RESTORE_NOTE, ringsNote, SPEEDS, speedStep,
  usesColor as colorEffect, usesSpeed as speedEffect } from "../desktop/native/rgb.mjs";

interface RgbSettings {
  control_enabled: boolean;
  rings_enabled: boolean;
  effect: string;
  hue: number;
  saturation: number;
  brightness: number;
  speed: number;
  power_led_managed: boolean;
  power_led_enabled: boolean;
}

interface RgbSnapshot {
  enabled?: boolean;
  effect?: string;
  brightness?: number;
  speed?: number;
  rgb?: number[];
}

interface RgbEffect {
  id: string;
  label: string;
}

export interface RgbStatus {
  settings?: RgbSettings;
  rgb?: {
    supported: boolean;
    reason?: string;
    current?: RgbSnapshot | null;
    effects?: RgbEffect[];
    drift?: boolean;
  };
  power_led?: {
    supported: boolean;
    reason?: string;
    current?: boolean | null;
    managed?: boolean;
    drift?: boolean;
    beta?: boolean;
  };
  recovery_required?: boolean;
  error?: string;
}

// A change answers D2 itself: the new status, also with a refusal (a refusal before any change has none).
type RgbResult = Result<RgbStatus>;
const UNCONFIRMED = "The change could not be confirmed. Reopen Lighting to check the current state.";

const mutations = mutationQueue();
const readRgbStatus = rpc<[], RgbStatus>("rgb_get_status");
export const getRgbStatus = async () => {
  if (mutations.pending) await mutations.tail;
  return readRgbStatus();
};
const setControlEnabled = callable<[boolean], RgbResult>("rgb_set_control_enabled");
const setRingsEnabled = callable<[boolean], RgbResult>("rgb_set_rings_enabled");
const setEffect = callable<[string], RgbResult>("rgb_set_effect");
const setColor = callable<[number, number], RgbResult>("rgb_set_color");
const setBrightness = callable<[number], RgbResult>("rgb_set_brightness");
const setSpeed = callable<[number], RgbResult>("rgb_set_speed");
const setPowerLed = callable<[boolean], RgbResult>("rgb_set_power_led");
const restoreOriginal = callable<[], RgbResult>("rgb_restore_original");

const EFFECT_LABELS: Record<string, string> = {
  monocolor: "Solid",
  breathe: "Breathing",
  chroma: "Color cycle",
  rainbow: "Rainbow",
};

type Gesture = "color" | "brightness" | "speed";

const effectLabel = (effect?: string) => EFFECT_LABELS[effect ?? ""] ?? "Custom";

/** powerLight: the console has the power button light (get_version.device.power_led). */
export const rgbSummary = (status?: RgbStatus, powerLight = true) => {
  if (!status?.settings) return powerLight ? "Stick lights and power button light" : "Stick lights";
  const lighting = status.settings.control_enabled
    ? status.settings.rings_enabled
      ? effectLabel(status.settings.effect)
      : "Stick lights off"
    : "Lighting control off";
  const power = status.power_led?.current;
  return powerLight ? `${lighting} · Power light ${power == null ? "unknown" : power ? "on" : "off"}` : lighting;
};

const hsvCss = (hue: number, saturation: number, value: number) => {
  const h = ((hue % 360) + 360) % 360;
  const s = Math.max(0, Math.min(100, saturation)) / 100;
  const v = Math.max(0, Math.min(100, value)) / 100;
  const c = v * s;
  const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
  const m = v - c;
  let r = 0;
  let g = 0;
  let b = 0;
  if (h < 60) [r, g, b] = [c, x, 0];
  else if (h < 120) [r, g, b] = [x, c, 0];
  else if (h < 180) [r, g, b] = [0, c, x];
  else if (h < 240) [r, g, b] = [0, x, c];
  else if (h < 300) [r, g, b] = [x, 0, c];
  else [r, g, b] = [c, 0, x];
  return `rgb(${Math.round((r + m) * 255)}, ${Math.round((g + m) * 255)}, ${Math.round((b + m) * 255)})`;
};

const RGBSliderStyles: FC<{ hue: number; saturation: number }> = ({
  hue,
  saturation,
}) => (
  <style>{`
    .LegionGo2RgbHue .${gamepadSliderClasses.SliderTrack} {
      background: linear-gradient(to right,
        hsl(0,100%,50%), hsl(60,100%,50%), hsl(120,100%,50%),
        hsl(180,100%,50%), hsl(240,100%,50%), hsl(300,100%,50%),
        hsl(360,100%,50%)) !important;
      --left-track-color: transparent !important;
      --colored-toggles-main-color: transparent !important;
    }
    .LegionGo2RgbSaturation .${gamepadSliderClasses.SliderTrack} {
      background: linear-gradient(to right, white, hsl(${hue},100%,50%)) !important;
      --left-track-color: transparent !important;
      --colored-toggles-main-color: transparent !important;
    }
    .LegionGo2RgbBrightness .${gamepadSliderClasses.SliderTrack} {
      background: linear-gradient(to right, black, hsl(${hue},${saturation}%,50%)) !important;
      --left-track-color: transparent !important;
      --colored-toggles-main-color: transparent !important;
    }
  `}</style>
);

export const RgbPage: FC<{ visible?: boolean; powerLight?: boolean }> = ({ visible: tabShown = true, powerLight = true }) => {
  const visible = useQuickAccessVisible() && tabShown;
  const [status, setStatus] = useState<RgbStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  // The control whose last change failed shows that error until the next change.
  const [failed, setFailed] = useState<{control: string; text: string} | null>(null);
  const pendingCount = useRef(0);
  // Debounced slider gestures, one per kind: timer, flush callback and version.
  const pendingWrites = useRef<Partial<Record<Gesture, () => void>>>({});
  const gestureTimers = useRef<Partial<Record<Gesture, ReturnType<typeof setTimeout>>>>({});
  const gestureVersions = useRef<Partial<Record<Gesture, number>>>({});
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
  const readInFlight = useRef(false);
  const refreshAfterRead = useRef(false);
  const drafts = useRef<Partial<RgbSettings>>({});
  const draftVersions = useRef<Partial<Record<keyof RgbSettings, number>>>({});
  const draftSequence = useRef(0);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);

  const timersPending = () => Object.keys(gestureTimers.current).length > 0;

  const flushPendingWrites = useCallback(() => {
    for (const timer of Object.values(gestureTimers.current)) clearTimeout(timer);
    gestureTimers.current = {};
    const writes = Object.values(pendingWrites.current);
    pendingWrites.current = {};
    for (const flush of writes) flush();
  }, []);

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || readInFlight.current || pendingCount.current > 0 || timersPending()) return;
    readInFlight.current = true;
    const revision = readRevision.current;
    try {
      // An older page may still be flushing accepted slider/button edits.
      // Its complete mutation tail must precede this page's status snapshot.
      if (mutations.pending) await mutations.tail;
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      const next = await getRgbStatus();
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      drafts.current = {}; draftVersions.current = {};
      setStatus(next);
      setError(next.error || "");
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current)
        setError("Lighting status is unavailable. Retrying while this page is open.");
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

  useEffect(() => flushPendingWrites, [flushPendingWrites]);

  const updateLocal = useCallback((patch: Partial<RgbSettings>) => {
    if (!mounted.current) return;
    readRevision.current += 1;
    Object.assign(drafts.current, patch);
    for (const key of Object.keys(patch) as Array<keyof RgbSettings>)
      draftVersions.current[key] = ++draftSequence.current;
    setStatus((current) => current?.settings ? {
      ...current,
      settings: { ...current.settings, ...patch },
    } : current);
  }, []);

  const enqueue = useCallback((operation: () => Promise<RgbResult>, control: string,
    fields: Array<keyof RgbSettings> = [], persistAfterClose = false) => {
    // Debounced gestures accepted while mounted still flush on close. New
    // callbacks from a discarded page must not initiate hardware changes.
    if (!mounted.current && !persistAfterClose) return Promise.resolve();
    readRevision.current += 1;
    flushPendingWrites();
    const versions = fields.map(key => [key, draftVersions.current[key]] as const);
    const settleDrafts = () => {
      for (const [key, version] of versions) if (version === draftVersions.current[key]) {
        delete drafts.current[key]; delete draftVersions.current[key];
      }
    };
    pendingCount.current += 1;
    if (mounted.current) setBusy(true);
    const run = async () => {
      let needsRefresh = false;
      if (mounted.current) { setError(""); setFailed(null); }
      const fail = (text: string) => { setFailed({ control, text }); toaster.toast({ title: "Lighting", body: text }); };
      try {
        const { ok, error, state } = await operation();
        if (!mounted.current) return;
        settleDrafts();
        if (state?.settings) {
          setStatus({ ...state, settings: { ...state.settings, ...drafts.current } });
        } else if (state) setStatus(state);
        else needsRefresh = true;
        if (!ok) fail(state ? error || "The lights did not confirm the change." : UNCONFIRMED);
        else if (control === "restore") toaster.toast({ title: "Lighting", body: "Original lighting restored." });
      } catch {
        needsRefresh = true;
        if (!mounted.current) return;
        settleDrafts();
        fail(UNCONFIRMED);
      } finally {
        mutations.done();
        pendingCount.current -= 1;
        if (mounted.current && pendingCount.current === 0) {
          setBusy(false);
          if (needsRefresh) void refresh();
        }
      }
    };
    return mutations.push(run);
  }, [flushPendingWrites, refresh]);

  // Show a slider gesture now; save only its final value after a short pause.
  // A superseded or already flushed callback cannot write again.
  const schedule = useCallback((gesture: Gesture, patch: Partial<RgbSettings>, write: () => Promise<RgbResult>) => {
    if (!mounted.current) return;
    updateLocal(patch);
    const previous = gestureTimers.current[gesture];
    if (previous != null) clearTimeout(previous);
    const version = gestureVersions.current[gesture] = (gestureVersions.current[gesture] ?? 0) + 1;
    let flushed = false;
    const flush = () => {
      if (flushed || version !== gestureVersions.current[gesture]) return;
      flushed = true;
      delete gestureTimers.current[gesture];
      delete pendingWrites.current[gesture];
      void enqueue(write, gesture, Object.keys(patch) as Array<keyof RgbSettings>, true);
    };
    pendingWrites.current[gesture] = flush;
    gestureTimers.current[gesture] = setTimeout(flush, 450);
  }, [enqueue, updateLocal]);
  const scheduleColor = (hue: number, saturation: number) => schedule("color", { hue, saturation }, () => setColor(hue, saturation));

  if (!status?.settings) return statusPlaceholder({ label: "Lighting status unavailable", error, onRetry: () => void refresh() });

  const settings = status.settings;
  const rgb = status.rgb;
  const power = status.power_led;
  const effectOptions: DropdownOption[] = (rgb?.effects ?? []).map((effect) => ({
    data: effect.id,
    label: effect.label,
  }));
  const selectedEffect = effectOptions.find(
    (option) => "data" in option && option.data === settings.effect,
  );
  const usesColor = colorEffect(settings);
  const usesSpeed = speedEffect(settings);
  const blocked = busy || status.recovery_required === true;
  const powerChecked = powerShown(status);
  const failure = (control: string) => failed?.control === control ? failed.text : undefined;
  const swatch = <span aria-label="Stick light color" style={{ display: "inline-block", width: "0.8em", height: "0.8em",
    borderRadius: "50%", marginRight: "6px", verticalAlign: "middle",
    background: hsvCss(settings.hue, settings.saturation, settings.brightness) }} />;

  return (
    <>
      <PanelSection title="Lighting">
        {status.recovery_required ? infoRow("Lighting is restoring its last state", "Controls unlock when it finishes.")
          : error && infoRow("Lighting status not updated", error)}
        <PanelSectionRow>
          <ToggleField
            label="Enable lighting control"
            description={failure("enabled") ?? controlNote(status)}
            checked={settings.control_enabled}
            disabled={blocked || rgb?.supported !== true}
            onChange={(value) => void enqueue(() => setControlEnabled(value), "enabled")}
          />
        </PanelSectionRow>
        {settings.control_enabled && (
          <PanelSectionRow>
            <ToggleField
              label="Stick lights"
              description={failure("rings") ?? (ringsNote(status) || undefined)}
              checked={settings.rings_enabled}
              disabled={blocked}
              onChange={(value) => void enqueue(() => setRingsEnabled(value), "rings")}
            />
          </PanelSectionRow>
        )}
        {effectsEditable(settings) && <>
          <PanelSectionRow>
            <DropdownItem
              label="Effect"
              description={failure("effect")}
              strDefaultLabel={effectLabel(settings.effect)}
              selectedOption={"data" in (selectedEffect ?? {}) ? selectedEffect?.data : settings.effect}
              rgOptions={effectOptions}
              disabled={blocked}
              onChange={(option) => {
                const effect = String(option.data);
                updateLocal({ effect });
                void enqueue(() => setEffect(effect), "effect", ["effect"]);
              }}
            />
          </PanelSectionRow>
          {usesColor && (
            <>
              <PanelSectionRow>
                <SliderField
                  className="LegionGo2RgbHue"
                  label="Hue"
                  description={failure("color") ?? <span>{swatch}Both stick lights.</span>}
                  value={settings.hue}
                  min={0}
                  max={359}
                  step={1}
                  showValue
                  valueSuffix="°"
                  disabled={blocked}
                  onChange={(value) => scheduleColor(Math.round(value), settings.saturation)}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <SliderField
                  className="LegionGo2RgbSaturation"
                  label="Saturation"
                  value={settings.saturation}
                  min={0}
                  max={100}
                  step={1}
                  showValue
                  valueSuffix="%"
                  disabled={blocked}
                  onChange={(value) => scheduleColor(settings.hue, Math.round(value))}
                />
              </PanelSectionRow>
            </>
          )}
          <PanelSectionRow>
            <SliderField
              className="LegionGo2RgbBrightness"
              label="Brightness"
              description={failure("brightness")}
              value={settings.brightness}
              min={0}
              max={100}
              step={1}
              showValue
              valueSuffix="%"
              disabled={blocked}
              onChange={(value) => { const brightness = Math.round(value); schedule("brightness", { brightness }, () => setBrightness(brightness)); }}
            />
          </PanelSectionRow>
          {usesSpeed && (
            <PanelSectionRow>
              <SliderField
                label="Speed"
                description={failure("speed")}
                value={speedStep(settings.speed)}
                min={0}
                max={100}
                step={50}
                notchCount={3}
                notchTicksVisible
                notchLabels={SPEEDS.map(({ id, label }, notchIndex) => ({ notchIndex, label, value: id }))}
                disabled={blocked}
                onChange={(value) => { const speed = Math.round(value); schedule("speed", { speed }, () => setSpeed(speed)); }}
              />
            </PanelSectionRow>
          )}
          <RGBSliderStyles hue={settings.hue} saturation={settings.saturation} />
        </>}
      </PanelSection>

      <PanelSection title={powerLight ? "Power button" : undefined}>
        {powerLight && <PanelSectionRow>
          <ToggleField
            label="Power button light"
            description={failure("power") ?? powerNote(status)}
            checked={powerChecked}
            disabled={blocked || power?.supported !== true}
            onChange={(value) => void enqueue(() => setPowerLed(value), "power")}
          />
        </PanelSectionRow>}
        {powerLight && power?.beta && infoRow("Beta", POWER_LED_BETA_TEXT)}
        {/* The page's last row is always enabled; the backend refuses it during recovery. */}
        <PanelSectionRow>
          <ButtonItem layout="below" description={failure("restore") ?? RESTORE_NOTE}
            onClick={() => void enqueue(restoreOriginal, "restore")}>
            Restore original lighting
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
