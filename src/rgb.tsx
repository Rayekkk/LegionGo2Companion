// SPDX-License-Identifier: BSD-3-Clause
// Copyright (c) 2026 Rayekkk

import { callable, toaster, useQuickAccessVisible } from "@decky/api";
import {
  ButtonItem,
  DropdownItem,
  DropdownOption,
  Field,
  gamepadSliderClasses,
  PanelSection,
  PanelSectionRow,
  SliderField,
  Spinner,
  ToggleField,
} from "@decky/ui";
import { FC, useCallback, useEffect, useRef, useState } from "react";

interface RgbSettings {
  configured: boolean;
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
  success: boolean;
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
    bios?: string;
  };
  recovery_required?: boolean;
  error?: string;
}

interface RgbResult {
  success: boolean;
  error?: string;
  status?: RgbStatus;
}

let rgbMutationChain: Promise<void> = Promise.resolve();
let rgbMutationsPending = 0;
const readRgbStatus = callable<[], RgbStatus>("rgb_get_status");
export const getRgbStatus = async () => {
  if (rgbMutationsPending) await rgbMutationChain;
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

const effectLabel = (effect?: string) => EFFECT_LABELS[effect ?? ""] ?? "Custom";

export const rgbSummary = (status?: RgbStatus) => {
  if (!status?.settings) return "Joystick rings and power button";
  const lighting = status.settings.control_enabled
    ? status.settings.rings_enabled
      ? effectLabel(status.settings.effect)
      : "Rings off"
    : "RGB control off";
  const power = status.power_led?.current;
  return `${lighting} · Power light ${power == null ? "unknown" : power ? "on" : "off"}`;
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

export const RgbPage: FC = () => {
  const visible = useQuickAccessVisible();
  const [status, setStatus] = useState<RgbStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const pendingCount = useRef(0);
  const pendingWrites = useRef<Record<string, () => void>>({});
  const colorTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const brightnessTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const speedTimer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
  const readInFlight = useRef(false);
  const refreshAfterRead = useRef(false);
  const drafts = useRef<Partial<RgbSettings>>({});
  const draftVersions = useRef<Partial<Record<keyof RgbSettings, number>>>({});
  const draftSequence = useRef(0);
  const colorVersion = useRef(0);
  const brightnessVersion = useRef(0);
  const speedVersion = useRef(0);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);

  const timersPending = () => colorTimer.current != null || brightnessTimer.current != null || speedTimer.current != null;

  const flushPendingWrites = useCallback(() => {
    for (const timer of [colorTimer, brightnessTimer, speedTimer]) {
      if (timer.current != null) clearTimeout(timer.current);
      timer.current = undefined;
    }
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
      if (rgbMutationsPending) await rgbMutationChain;
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

  const enqueue = useCallback((operation: () => Promise<RgbResult>, fields: Array<keyof RgbSettings> = [], persistAfterClose = false) => {
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
    rgbMutationsPending += 1;
    if (mounted.current) setBusy(true);
    const run = async () => {
      let needsRefresh = false;
      if (mounted.current) { setError(""); setNotice(""); }
      try {
        const result = await operation();
        if (!mounted.current) return;
        settleDrafts();
        if (result.status?.settings) {
          setStatus({ ...result.status, settings: { ...result.status.settings, ...drafts.current } });
        } else if (result.status) setStatus(result.status);
        else needsRefresh = true;
        if (result.success) setNotice("The lighting state was applied and saved.");
        else {
          setError(result.error || "The hardware did not confirm the change.");
          toaster.toast({ title: "Lighting change failed", body: result.error || "The hardware did not confirm the change." });
        }
      } catch {
        needsRefresh = true;
        if (!mounted.current) return;
        settleDrafts();
        setError("The backend call ended before the lighting state was verified.");
        toaster.toast({ title: "Lighting change failed", body: "The save could not be confirmed. Reopen Lighting to check the current state." });
      } finally {
        rgbMutationsPending -= 1;
        pendingCount.current -= 1;
        if (mounted.current && pendingCount.current === 0) {
          setBusy(false);
          if (needsRefresh) void refresh();
        }
      }
    };
    const queued = rgbMutationChain.then(run, run);
    rgbMutationChain = queued.catch(() => undefined);
    return queued;
  }, [flushPendingWrites, refresh]);

  const scheduleColor = useCallback((hue: number, saturation: number) => {
    if (!mounted.current) return;
    updateLocal({ hue, saturation });
    if (colorTimer.current) clearTimeout(colorTimer.current);
    const version = ++colorVersion.current;
    let flushed = false;
    const flush = () => {
      if (flushed || version !== colorVersion.current) return;
      flushed = true;
      colorTimer.current = undefined;
      delete pendingWrites.current.color;
      void enqueue(() => setColor(hue, saturation), ["hue", "saturation"], true);
    };
    pendingWrites.current.color = flush;
    colorTimer.current = setTimeout(flush, 450);
  }, [enqueue, updateLocal]);

  const scheduleBrightness = useCallback((brightness: number) => {
    if (!mounted.current) return;
    updateLocal({ brightness });
    if (brightnessTimer.current) clearTimeout(brightnessTimer.current);
    const version = ++brightnessVersion.current;
    let flushed = false;
    const flush = () => {
      if (flushed || version !== brightnessVersion.current) return;
      flushed = true;
      brightnessTimer.current = undefined;
      delete pendingWrites.current.brightness;
      void enqueue(() => setBrightness(brightness), ["brightness"], true);
    };
    pendingWrites.current.brightness = flush;
    brightnessTimer.current = setTimeout(flush, 450);
  }, [enqueue, updateLocal]);

  const scheduleSpeed = useCallback((speed: number) => {
    if (!mounted.current) return;
    updateLocal({ speed });
    if (speedTimer.current != null) clearTimeout(speedTimer.current);
    const version = ++speedVersion.current;
    let flushed = false;
    const flush = () => {
      if (flushed || version !== speedVersion.current) return;
      flushed = true;
      speedTimer.current = undefined;
      delete pendingWrites.current.speed;
      void enqueue(() => setSpeed(speed), ["speed"], true);
    };
    pendingWrites.current.speed = flush;
    speedTimer.current = setTimeout(flush, 450);
  }, [enqueue, updateLocal]);

  if (!status?.settings) {
    return <PanelSection><PanelSectionRow>{error
      ? <Field focusable label="Lighting status unavailable" description={error} />
      : <Spinner />}</PanelSectionRow>
      {error && <PanelSectionRow><ButtonItem layout="below" onClick={() => void refresh()}>Retry status</ButtonItem></PanelSectionRow>}
    </PanelSection>;
  }

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
  const usesColor = settings.effect === "monocolor" || settings.effect === "breathe";
  const usesSpeed = settings.effect !== "monocolor";
  const blocked = busy || status.recovery_required === true;
  const powerChecked = settings.power_led_managed
    ? settings.power_led_enabled
    : power?.current ?? true;
  const canRestore = settings.control_enabled || settings.power_led_managed;

  return (
    <>
      {status.recovery_required && (
        <PanelSection title="Recovery required">
          <PanelSectionRow>
            <Field
              focusable
              label="Lighting controls are temporarily locked"
              description="Companion is restoring the last complete hardware state after an interrupted change."
            />
          </PanelSectionRow>
        </PanelSection>
      )}

      <PanelSection title="RGB basic settings">
        <PanelSectionRow>
          <ToggleField
            label="Enable RGB control"
            description="Let Companion manage the joystick-ring lights and restore their saved state after startup or wake."
            checked={settings.control_enabled}
            disabled={blocked || rgb?.supported !== true}
            onChange={(value) => void enqueue(() => setControlEnabled(value))}
          />
        </PanelSectionRow>
        {rgb?.supported !== true && (
          <PanelSectionRow>
            <Field focusable label="Joystick-ring control unavailable" description={rgb?.reason ?? "The controller interface was not detected."} />
          </PanelSectionRow>
        )}
        {settings.control_enabled && (
          <PanelSectionRow>
            <ToggleField
              label="Joystick ring lights"
              description="Turn both controller rings on or off."
              checked={settings.rings_enabled}
              disabled={blocked}
              onChange={(value) => void enqueue(() => setRingsEnabled(value))}
            />
          </PanelSectionRow>
        )}
        {settings.control_enabled && settings.rings_enabled && (
          <PanelSectionRow>
            <DropdownItem
              label="Lighting effect"
              strDefaultLabel={effectLabel(settings.effect)}
              selectedOption={"data" in (selectedEffect ?? {}) ? selectedEffect?.data : settings.effect}
              rgOptions={effectOptions}
              disabled={blocked}
              onChange={(option) => {
                const effect = String(option.data);
                updateLocal({ effect });
                void enqueue(() => setEffect(effect), ["effect"]);
              }}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      {settings.control_enabled && settings.rings_enabled && (
        <PanelSection title="Primary Zone">
          <PanelSectionRow>
            <Field focusable label="Selected color" description={usesColor ? "Preview for both controller rings. Used by Solid and Breathing effects." : "This hardware effect cycles its own colors."}>
              <span aria-label="Selected joystick ring color preview" style={{ display: "flex", gap: "12px" }}>
                {["Left", "Right"].map(side => <span key={side} role="img" aria-label={`${side} joystick ring preview`}
                  style={{ display: "block", width: "1.8em", height: "1.8em", borderRadius: "50%",
                    border: `3px solid ${hsvCss(settings.hue, settings.saturation, settings.brightness)}`,
                    opacity: settings.control_enabled && settings.rings_enabled ? 1 : 0.35 }} />)}
              </span>
            </Field>
          </PanelSectionRow>
          {usesColor && (
            <>
              <PanelSectionRow>
                <SliderField
                  className="LegionGo2RgbHue"
                  label="Hue"
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
              value={settings.brightness}
              min={0}
              max={100}
              step={1}
              showValue
              valueSuffix="%"
              disabled={blocked}
              onChange={(value) => scheduleBrightness(Math.round(value))}
            />
          </PanelSectionRow>
          {usesSpeed && (
            <PanelSectionRow>
              <SliderField
                label="Speed"
                description={settings.speed <= 25 ? "Slow" : settings.speed >= 75 ? "Fast" : "Medium"}
                value={settings.speed <= 25 ? 0 : settings.speed >= 75 ? 100 : 50}
                min={0}
                max={100}
                step={50}
                notchCount={3}
                notchTicksVisible
                notchLabels={[
                  { notchIndex: 0, label: "Slow", value: 0 },
                  { notchIndex: 1, label: "Medium", value: 50 },
                  { notchIndex: 2, label: "Fast", value: 100 },
                ]}
                disabled={blocked}
                onChange={(value) => scheduleSpeed(Math.round(value))}
              />
            </PanelSectionRow>
          )}
          <RGBSliderStyles hue={settings.hue} saturation={settings.saturation} />
        </PanelSection>
      )}

      <PanelSection title="Power button">
        <PanelSectionRow>
          <ToggleField
            label="Power button light"
            description={power?.supported
              ? `Use the power-button LED state on this audited Go 2 BIOS${power.bios ? ` (${power.bios})` : ""}.`
              : power?.reason ?? "The power-button interface is unavailable."}
            checked={powerChecked}
            disabled={blocked || power?.supported !== true}
            onChange={(value) => void enqueue(() => setPowerLed(value))}
          />
        </PanelSectionRow>
        {power?.drift && (
          <PanelSectionRow>
            <Field focusable label="Power light setting drifted" description="Companion will restore the saved state automatically." />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Safety">
        <PanelSectionRow>
          <Field
            focusable
            label="Hardware effects only"
            description="Animations run in controller firmware. Companion performs no frame-by-frame RGB work and only checks for drift once per minute."
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={blocked || !canRestore}
            onClick={() => void enqueue(restoreOriginal)}
          >
            Restore lighting state from before Companion
          </ButtonItem>
        </PanelSectionRow>
        {(error || notice || rgb?.drift) && (
          <PanelSectionRow>
            <Field
              focusable
              label={error ? "Could not complete" : rgb?.drift ? "Restoring saved RGB state" : "Result"}
              description={error || (rgb?.drift ? "A different component changed the joystick rings; Companion will reconcile them." : notice)}
            />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
};
