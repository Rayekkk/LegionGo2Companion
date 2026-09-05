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

export const getRgbStatus = callable<[], RgbStatus>("rgb_get_status");
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

let rgbMutationChain: Promise<void> = Promise.resolve();

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
  const mounted = useRef(true);
  const isVisible = useRef(visible);
  const readRevision = useRef(0);
  const readInFlight = useRef(false);
  isVisible.current = visible;
  useEffect(() => { mounted.current = true; return () => {
    mounted.current = false; readRevision.current += 1;
  }; }, []);

  const timersPending = () => colorTimer.current != null || brightnessTimer.current != null;

  const refresh = useCallback(async () => {
    if (!mounted.current || !isVisible.current || readInFlight.current || pendingCount.current > 0 || timersPending()) return;
    readInFlight.current = true;
    const revision = readRevision.current;
    try {
      const next = await getRgbStatus();
      if (!mounted.current || !isVisible.current || revision !== readRevision.current) return;
      setStatus(next);
      setError(next.error || "");
    } catch {
      if (mounted.current && isVisible.current && revision === readRevision.current)
        setError("Lighting status is unavailable. Retrying while this page is open.");
    } finally { readInFlight.current = false; }
  }, []);

  useEffect(() => {
    if (!visible) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 10000);
    return () => { clearInterval(timer); readRevision.current += 1; };
  }, [refresh, visible]);

  useEffect(() => () => {
    if (colorTimer.current) clearTimeout(colorTimer.current);
    if (brightnessTimer.current) clearTimeout(brightnessTimer.current);
    const writes = Object.values(pendingWrites.current);
    pendingWrites.current = {};
    for (const flush of writes) flush();
  }, []);

  const updateLocal = useCallback((patch: Partial<RgbSettings>) => {
    readRevision.current += 1;
    setStatus((current) => current?.settings ? {
      ...current,
      settings: { ...current.settings, ...patch },
    } : current);
  }, []);

  const enqueue = useCallback((operation: () => Promise<RgbResult>) => {
    readRevision.current += 1;
    const pending = Object.values(pendingWrites.current);
    pendingWrites.current = {};
    if (colorTimer.current) clearTimeout(colorTimer.current);
    if (brightnessTimer.current) clearTimeout(brightnessTimer.current);
    for (const flush of pending) flush();
    pendingCount.current += 1;
    const run = async () => {
      setBusy(true);
      setError("");
      setNotice("");
      try {
        const result = await operation();
        if (result.status && mounted.current) setStatus(result.status);
        if (result.success) setNotice("The lighting state was applied and saved.");
        else {
          setError(result.error || "The hardware did not confirm the change.");
          toaster.toast({ title: "Lighting change failed", body: result.error || "The hardware did not confirm the change." });
        }
      } catch {
        setError("The backend call ended before the lighting state was verified.");
        toaster.toast({ title: "Lighting change failed", body: "The save could not be confirmed. Reopen Lighting to check the current state." });
      } finally {
        pendingCount.current -= 1;
        if (mounted.current && pendingCount.current === 0) setBusy(false);
      }
    };
    const queued = rgbMutationChain.then(run, run);
    rgbMutationChain = queued.catch(() => undefined);
    return queued;
  }, []);

  const scheduleColor = useCallback((hue: number, saturation: number) => {
    updateLocal({ hue, saturation });
    if (colorTimer.current) clearTimeout(colorTimer.current);
    const flush = () => {
      colorTimer.current = undefined;
      delete pendingWrites.current.color;
      void enqueue(() => setColor(hue, saturation));
    };
    pendingWrites.current.color = flush;
    colorTimer.current = setTimeout(flush, 450);
  }, [enqueue, updateLocal]);

  const scheduleBrightness = useCallback((brightness: number) => {
    updateLocal({ brightness });
    if (brightnessTimer.current) clearTimeout(brightnessTimer.current);
    const flush = () => {
      brightnessTimer.current = undefined;
      delete pendingWrites.current.brightness;
      void enqueue(() => setBrightness(brightness));
    };
    pendingWrites.current.brightness = flush;
    brightnessTimer.current = setTimeout(flush, 450);
  }, [enqueue, updateLocal]);

  if (!status?.settings) {
    return <PanelSection><PanelSectionRow><Spinner /></PanelSectionRow></PanelSection>;
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
            <Field label="Joystick-ring control unavailable" description={rgb?.reason ?? "The controller interface was not detected."} />
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
                void enqueue(() => setEffect(effect));
              }}
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      {settings.control_enabled && settings.rings_enabled && (
        <PanelSection title="Primary Zone">
          <PanelSectionRow>
            <Field label="Selected color" description={usesColor ? "Used by Solid and Breathing effects." : "This hardware effect cycles its own colors."}>
              <span style={{
                display: "block",
                width: "2.4em",
                height: "2.4em",
                borderRadius: "50%",
                background: hsvCss(settings.hue, settings.saturation, settings.brightness),
                border: "2px solid rgba(255,255,255,.65)",
                boxShadow: `0 0 12px ${hsvCss(settings.hue, settings.saturation, settings.brightness)}`,
              }} />
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
                onChange={(value) => {
                  const speed = Math.round(value);
                  updateLocal({ speed });
                  void enqueue(() => setSpeed(speed));
                }}
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
            <Field label="Power light setting drifted" description="Companion will restore the saved state automatically." />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Safety">
        <PanelSectionRow>
          <Field
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
              label={error ? "Could not complete" : rgb?.drift ? "Restoring saved RGB state" : "Result"}
              description={error || (rgb?.drift ? "A different component changed the joystick rings; Companion will reconcile them." : notice)}
            />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
};
