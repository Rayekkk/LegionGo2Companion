// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/frontend/curve.tsx): change it there, then `kit sync`.
//
// Sources: DeckyEQ src/editor.tsx (only A starts editing, every direction stays in the graph while editing, other
// buttons are taken only when the graph uses them, a grabbed dot moves by as much as the finger and never jumps,
// margins from the font size) and src/App.tsx (the full-size dialog), LegionGo2Companion src/fan.tsx (viewBox from
// the measured frame in full screen, pointer capture, the legend while editing, the dialog's styles), docs/UI.md §8.
// Guarantees: the graph is one Focusable; A starts editing (not a tap or a drag; a tap too only with `tapToEdit`, as
// Companion's fan curve always did), B and leaving the graph end it, and nothing starts editing while disabled,
// and while it is not being edited it takes no button, so the D-pad moves on; while editing every direction is
// given to `press` and kept, any other button only when `press` used it; Steam's legend shows "Edit curve", then
// "Done" and the plugin's buttons; a drag follows one pointer (captured), reports points in viewBox units together
// with where it started (exact also when the viewBox is letterboxed), ends on release, cancel or lost capture, and
// nothing moves while disabled; in full screen the viewBox has the frame's aspect down to `minWidth`, so text keeps its
// size; colors come from ui.tsx, the accent is the plugin's (`accent`, Steam's blue by default).
// Deliberately not here: the data, scales (°C, log Hz), the curve, its labels and what each button does.
import { DialogHeader, Focusable, GamepadButton, GamepadEvent, ModalRoot } from "@decky/ui";
import { PointerEvent, ReactNode, useEffect, useRef, useState } from "react";
import { colors } from "./ui";

export interface Frame { width: number; height: number }
export interface Point { x: number; y: number }
export interface CurveLayout extends Frame { scale: number; font: number; left: number; right: number; top: number; bottom: number }
/** The QAM's fixed viewBox; full screen's viewBox height, minimum width and font in screen px; margins in font sizes. */
export interface CurveSize {
  qam: Frame & { font: number };
  full: { height: number; minWidth: number; font: number };
  margins: { left: number; right: number; top: number; bottom: number };
}
export const CURVE_SIZE: CurveSize = { qam: { width: 300, height: 180, font: 11 }, full: { height: 300, minWidth: 600, font: 16 },
  margins: { left: 2.4, right: 0.8, top: 1.8, bottom: 1.6 } };

/** viewBox geometry: fixed in the QAM; in full screen as wide as the frame it gets, so text keeps `full.font` px. */
export const curveLayout = (fullscreen: boolean, frame: Frame = { width: 800, height: 300 }, size = CURVE_SIZE): CurveLayout => {
  const { qam, full, margins: m } = size;
  const view = fullscreen ? { width: Math.max(full.minWidth, Math.round(frame.width / frame.height * full.height)), height: full.height }
    : { width: qam.width, height: qam.height };
  const scale = fullscreen ? view.width / frame.width : 1, font = fullscreen ? full.font * scale : qam.font;
  return { ...view, scale, font, left: font * m.left, right: font * m.right, top: font * m.top, bottom: view.height - font * m.bottom };
};

const isDirection = (button: number) => button >= GamepadButton.DIR_UP && button <= GamepadButton.DIR_RIGHT;

/** Grid lines across the plot with their labels at the left (`strong`: the reference line), and labels under it. */
export function CurveAxes({ layout: L, x, y, rows, columns }: {
  layout: CurveLayout; x(value: number): number; y(value: number): number;
  rows: Array<{ value: number; label: string; strong?: boolean }>; columns: Array<{ value: number; label: string }>;
}) {
  return <>
    {rows.map(r => <g key={r.value}>
      <line x1={L.left} x2={L.width - L.right} y1={y(r.value)} y2={y(r.value)} stroke={r.strong ? colors.curve.zero : colors.curve.grid} />
      <text x={L.left - L.font * 0.4} y={y(r.value) + L.font * 0.35} textAnchor="end" fill="currentColor" fontSize={L.font}>{r.label}</text>
    </g>)}
    {columns.map(c => <text key={c.value} x={x(c.value)} y={L.height - L.font * 0.4} textAnchor="middle" fill="currentColor"
      fontSize={L.font}>{c.label}</text>)}
  </>;
}

export interface CurveView<H> {
  layout: CurveLayout;
  steering: boolean;
  /** For a handle's onPointerDown: starts dragging it (not while disabled). */
  grab(evt: PointerEvent, handle: H): void;
}

/**
 * The graph's frame: focus, editing with the gamepad, touch and full screen. The plugin draws inside (`children`)
 * and decides what each button does (`press`) and where a dragged handle goes (`drag`).
 */
export function CurveGraph<H>({ fullscreen = false, disabled, label, className, size, accent = colors.accent, tapToEdit = false, hint, legend = {}, press, drag, children }: {
  fullscreen?: boolean; disabled: boolean;
  /** A tap (Steam's onActivate) starts editing as A does; off by default, where a tap or a drag never does. */
  tapToEdit?: boolean;
  /** What the graph is and how to use it, for screen readers. */
  label: string; className?: string; size?: CurveSize;
  /** The plugin's accent for the frame and hint while editing (its curve uses the same one); Steam's blue by default. */
  accent?: string;
  /** The line above the graph: how to start, then while editing what each button does. */
  hint(steering: boolean): ReactNode;
  /** Steam's legend for the plugin's own buttons while editing (GamepadButton → text). */
  legend?: Record<number, string>;
  /** A button while editing: true when the graph used it. */
  press(button: number): boolean;
  /** A grabbed handle moved: where the pointer is and where it was grabbed, in viewBox units of `layout`. */
  drag?(handle: H, at: Point, from: Point, layout: CurveLayout): void;
  children(view: CurveView<H>): ReactNode;
}) {
  const [steering, setSteering] = useState(false);
  const held = useRef<{ handle: H; pointer: number; from: Point } | null>(null);
  const svg = useRef<SVGSVGElement>(null);
  const [frame, setFrame] = useState<Frame>({ width: 800, height: 300 });
  useEffect(() => {
    const element = svg.current;
    if (!fullscreen || !element) return;
    const update = () => {
      const r = element.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) setFrame(p => Math.abs(p.width - r.width) < 0.5 && Math.abs(p.height - r.height) < 0.5 ? p : { width: r.width, height: r.height });
    };
    const Observer = element.ownerDocument.defaultView?.ResizeObserver, observer = Observer ? new Observer(update) : null;
    observer?.observe(element);
    update();
    return () => observer?.disconnect();
  }, [fullscreen]);
  const layout = curveLayout(fullscreen, frame, size);
  /** The pointer in viewBox units; null while the graph has no size. Through the screen matrix where there is one:
   *  a frame narrower than minWidth letterboxes the viewBox, and a plain box mapping would drift from the finger. */
  const at = (evt: PointerEvent): Point | null => {
    const element = svg.current, box = element?.getBoundingClientRect();
    if (!element || !box?.width || !box.height) return null;
    const matrix = element.getScreenCTM?.(), point = matrix && element.createSVGPoint?.();
    if (matrix && point) {
      point.x = evt.clientX; point.y = evt.clientY;
      const local = point.matrixTransform(matrix.inverse());
      return { x: local.x, y: local.y };
    }
    return { x: (evt.clientX - box.left) / box.width * layout.width, y: (evt.clientY - box.top) / box.height * layout.height };
  };
  const grab = (evt: PointerEvent, handle: H) => {
    evt.preventDefault();
    const from = at(evt);
    if (disabled || !from) return;
    held.current = { handle, pointer: evt.pointerId, from };
    svg.current?.setPointerCapture?.(evt.pointerId);
  };
  const move = (evt: PointerEvent<SVGSVGElement>) => {
    const point = at(evt), grip = held.current;
    if (disabled || !point || !grip || grip.pointer !== evt.pointerId) return;
    drag?.(grip.handle, point, grip.from, layout);
  };
  const stop = () => { held.current = null; };
  const consume = (evt: GamepadEvent) => { evt.preventDefault(); evt.stopPropagation(); };
  const enter = (evt: GamepadEvent) => { if (disabled) return; consume(evt); setSteering(true); };
  const onDirection = (evt: GamepadEvent) => {
    if (!isDirection(evt.detail.button) || disabled) return;
    consume(evt);  // Every direction stays in the graph while editing, so the focus never wanders off.
    press(evt.detail.button);
  };
  const onButton = (evt: GamepadEvent) => {
    const button = evt.detail.button;
    if (isDirection(button) || disabled || !press(button)) return;  // Directions arrive through onGamepadDirection.
    consume(evt);
  };
  // Only A starts editing unless `tapToEdit`: a tap or a drag (Steam's onActivate) must not. The empty onActivate keeps it focusable.
  return <Focusable className={className} onActivate={tapToEdit ? enter : () => {}} onOKButton={enter}
    onCancelButton={steering ? evt => { consume(evt); setSteering(false); } : undefined}
    // Only while editing: Steam keeps the focus on any element that handles directions (seen on the Legion).
    onButtonDown={steering ? onButton : undefined} onGamepadDirection={steering ? onDirection : undefined}
    onGamepadBlur={() => setSteering(false)}
    onOKActionDescription={steering ? undefined : "Edit curve"} onCancelActionDescription={steering ? "Done" : undefined}
    actionDescriptionMap={steering ? legend : {}} focusClassName="dk-curve-focused"
    style={{ border: `2px solid ${steering ? accent : colors.curve.border}`, borderRadius: 8, width: "100%", boxSizing: "border-box",
      background: colors.surface, color: colors.curve.label, ...(fullscreen ? { height: "100%", display: "flex", flexDirection: "column" } : {}) }}>
    <style>{".dk-curve-focused{outline:2px solid #fff;outline-offset:2px}"}</style>
    <div style={{ padding: "6px 10px", fontSize: fullscreen ? 16 : 12, color: steering ? accent : colors.text }}>{hint(steering)}</div>
    <svg ref={svg} viewBox={`0 0 ${layout.width} ${layout.height}`} role="img" aria-label={label}
      style={{ display: "block", width: "100%", touchAction: "none", ...(fullscreen ? { flex: 1, minHeight: 0 } : {}) }}
      onPointerMove={move} onPointerUp={evt => { stop(); svg.current?.releasePointerCapture?.(evt.pointerId); }}
      onPointerCancel={stop} onLostPointerCapture={stop}>
      {children({ layout, steering, grab })}
    </svg>
  </Focusable>;
}

/** Full-size dialog styles, as Companion's fan curve editor sizes Steam's dialog. */
const dialogStyles = `
.dk-curve-dialog { width:min(1120px,94vw)!important; max-width:94vw!important; height:100%!important; max-height:900px!important; min-height:0!important;
  display:flex!important; flex-direction:column!important; flex:1 1 auto!important; overflow:hidden!important; box-sizing:border-box!important; padding:20px!important; }
.dk-curve-dialog-layer { padding:16px!important; overflow:hidden!important; align-items:center!important; justify-content:center!important; }
.dk-curve-dialog button { min-height:40px; }
.dk-curve-dialog * { box-sizing:border-box; }
`;

/** The same graph in full screen: a Steam dialog at full size with a title; `children` is the plugin's body. */
export const CurveDialog = ({ title, closeModal, children }: { title: string; closeModal?(): void; children: ReactNode }) =>
  <ModalRoot bAllowFullSize className="dk-curve-dialog" modalClassName="dk-curve-dialog-layer" onCancel={closeModal} closeModal={closeModal}>
    <style>{dialogStyles}</style>
    <DialogHeader>{title}</DialogHeader>
    {children}
  </ModalRoot>;
