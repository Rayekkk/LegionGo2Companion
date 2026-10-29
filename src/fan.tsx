// SPDX-License-Identifier: BSD-3-Clause
import { useQuickAccessVisible } from "@decky/api";
import { ButtonItem, PanelSection, PanelSectionRow, ToggleField, Focusable, GamepadButton,
  DropdownItem, Dropdown, DialogBody, DialogLabel, DialogFooter, DialogButton, showModal, Spinner } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";
import { CurveAxes, CurveDialog, CurveGraph as Graph, curveLayout, type CurveLayout, type CurveSize, type Point } from "./curve";
import { errorText, rpc } from "./shared";
import { colors, infoRow, notify as toast } from "./ui";
import { curveName as title, editPoint, fanStatus, nudgeSpeed, pointText, saveLabel, speedText } from "../desktop/native/fan.mjs";
import { FAN_BETA_TEXT } from "../desktop/native/modules.mjs";

export type Curve = [number, number][];
export interface FanState {
  supported: boolean; beta?: boolean; error: string; active_curve: string;
  needs_curve_review?: boolean;
  settings: { enabled: boolean; revision: number; assign_tdp_presets?: boolean; display_unit?: "rpm" | "percent"; global_curve: Curve; presets: Record<string, Curve> };
  templates?: Record<string, Curve>;
  limits: { min_rpm: number; last_min_rpm: number; max_rpm: number; step: number; hard_max: number };
  ownership?: {blocked: boolean; state: string; reason: string};
  presets: string[];
  readings?: { valid: boolean; rpm?: number; apu_temperature?: number; age_seconds?: number;
    automatic?: boolean; source?: string; error: string };
  receivedAt?: number;
  telemetry?: { active: boolean; automatic: boolean; error?: string; rpm?: number; target?: number;
    override?: number; effective?: number; stop_verified?: boolean; temperatures?: Record<string, number> };
}
const readState = rpc<[], FanState>("fan_get_state");
const requestEnabled = rpc<[boolean], FanState>("fan_set_enabled");
const requestCurve = rpc<[string, Curve | null, number], FanState>("fan_save_curve");
const requestUnit = rpc<["rpm" | "percent", number], FanState>("fan_save_unit");
const requestAssignments = rpc<[boolean, number], FanState>("fan_save_assignment_mode");
const timed = async (operation: () => Promise<FanState>) => {
  const started = Date.now(), next = await operation(), receivedAt = Date.now();
  return { ...next, receivedAt, readings: next.readings && { ...next.readings,
    age_seconds: (next.readings.age_seconds ?? 0) + Math.max(0, receivedAt-started)/1000 } };
};
const setEnabled = (enabled: boolean) => timed(() => requestEnabled(enabled));
const saveCurve = (scope: string, curve: Curve | null, revision: number) => timed(() => requestCurve(scope, curve, revision));
const saveUnit = (unit: "rpm" | "percent", revision: number) => timed(() => requestUnit(unit, revision));
const saveAssignments = (enabled: boolean, revision: number) => timed(() => requestAssignments(enabled, revision));
const ASSIGNMENT_NOTE = "Each Companion TDP preset can have its own curve (not Steam's Performance setting). Others use the global curve.";
let chain: Promise<void> = Promise.resolve();
export const getFanState = async (wanted = () => true) => {
  await chain;
  if (!wanted()) throw new Error("Fan view is no longer visible.");
  return timed(readState);
};
const ordered = (operation: () => Promise<FanState>) => {
  const result = chain.then(operation);
  chain = result.then(() => {}, () => {});
  return result;
};
const clone = (curve: Curve): Curve => curve.map(([t, r]) => [t, r]);
export const speedLabel = (rpm: number | undefined, state: Pick<FanState, "settings" | "limits">) =>
  speedText(rpm, state.settings.display_unit, state.limits?.max_rpm);
const RPM_EDIT_STEPS = [1,10,50,100,200,500];
const PERCENT_EDIT_STEPS = [1,2,5,10];
const speedEditStep = (rpmStep: number, state: Pick<FanState, "settings" | "limits">) => {
  const percent = state.settings.display_unit === "percent";
  const options = percent ? PERCENT_EDIT_STEPS : RPM_EDIT_STEPS;
  const wanted = percent ? rpmStep/state.limits.max_rpm*100 : rpmStep;
  return options.reduce((best, value) => Math.abs(value-wanted) < Math.abs(best-wanted) ? value : best);
};
const fanReadingLabel = (state?: FanState, now = Date.now()) => {
  const r = state?.readings;
  const age = (r?.age_seconds ?? Infinity) + (now-(state?.receivedAt ?? -Infinity))/1000;
  if (!r?.valid || !Number.isFinite(age) || age < 0 || age > 2.5 ||
      !Number.isFinite(r.apu_temperature) || !Number.isInteger(r.rpm)) return `APU — · ${state ? speedLabel(undefined, state) : "— RPM"}`;
  return `APU ${r.apu_temperature!.toFixed(1)}°C · ${speedLabel(r.rpm, state!)}`;
};
export { editPoint, fanStatus };
const fanMode = (state?: FanState) => fanStatus(state).headline;
export const fanSummary = (state?: FanState) => `${fanReadingLabel(state)} · ${fanMode(state)}`;
const notify = (body: string) => toast("Fan Control", body);

/** Legion's geometry on curve.tsx: wide left margins hold "4800 RPM", the bottom one the degrees and the point line. */
const FAN_SIZE: CurveSize = { qam: { width: 400, height: 248, font: 12 }, full: { height: 308, minWidth: 800, font: 18 },
  margins: { left: 6.4, right: 2.6, top: 2.6, bottom: 3.8 } };
export const graphLayout = (fullscreen: boolean, width = 800, height = 308) => curveLayout(fullscreen, { width, height }, FAN_SIZE);
const FAN_LEGEND = { [GamepadButton.BUMPER_LEFT]: "Previous point", [GamepadButton.BUMPER_RIGHT]: "Next point" };
export const CurveGraph: FC<{curve: Curve; selected: number; status: FanState; disabled: boolean; rpmStep: number;
  fullscreen?: boolean;
  select: (point: number) => void; edit: (axis: 0|1, value: number, point?: number) => void}> = ({curve, selected, status, disabled, rpmStep, select, edit, fullscreen=false}) => {
  const max = status.limits.max_rpm;
  /** A gamepad button while editing: LB/RB pick a point, left/right move it 1°C, up/down change its speed by one step. */
  const press = (button: number) => {
    if (button === GamepadButton.BUMPER_LEFT || button === GamepadButton.BUMPER_RIGHT) {
      select((selected+(button === GamepadButton.BUMPER_LEFT ? 7 : 1))%8); return true;
    }
    if (button === GamepadButton.DIR_LEFT || button === GamepadButton.DIR_RIGHT) { edit(0, curve[selected][0]+(button === GamepadButton.DIR_LEFT ? -1 : 1)); return true; }
    if (button === GamepadButton.DIR_UP || button === GamepadButton.DIR_DOWN) {
      edit(1, nudgeSpeed(curve[selected][1], button === GamepadButton.DIR_UP ? rpmStep : -rpmStep, status.limits.min_rpm)); return true;
    }
    return false;
  };
  const span = (L: CurveLayout) => L.width-L.left-L.right, rise = (L: CurveLayout) => L.bottom-L.top;
  // A dragged point goes where the finger is, on both axes.
  const drag = (point: number, at: Point, _from: Point, L: CurveLayout) => {
    edit(0,(at.x-L.left)*100/span(L),point);edit(1, Math.round((L.bottom-at.y)/rise(L)*max/rpmStep)*rpmStep,point);
  };
  const fresh = !fanReadingLabel(status).includes("—");
  // A tap starts editing as A does (Companion always worked so; DeckyEQ's graph starts only with A).
  return <Graph<number> fullscreen={fullscreen} disabled={disabled} size={FAN_SIZE} legend={FAN_LEGEND} press={press} drag={drag} tapToEdit
    label="Eight-point fan curve. Drag points or press A and use D-pad. LB/RB selects a point. Point eight stays at 100°C and cannot go below the minimum running speed."
    hint={steering => steering ? "Editing · LB/RB point · ←/→ 1°C · ↑/↓ speed · B done" : "Focus graph and press A to edit with controller"}>
    {({layout: L, grab}) => {
      const {left,font}=L;
      const x = (t: number) => left+t*span(L)/100, y = (r: number) => L.bottom-r/max*rise(L);
      return <>
        <rect x={left} y={y(status.limits.min_rpm)} width={span(L)} height={L.bottom-y(status.limits.min_rpm)} fill="currentColor" opacity=".05" />
        <CurveAxes layout={L} x={x} y={y} columns={[]}
          rows={[0,status.limits.min_rpm,max].map(r => ({value: r, label: speedLabel(r,status)}))} />
        {[0,20,40,60,80,100].map(t => <text key={t} x={x(t)} y={L.bottom+font*1.8} textAnchor="middle" fill="currentColor" fontSize={font}>{t}°</text>)}
        <path d={`M${left} ${L.top}V${L.bottom}H${L.width-L.right}`} stroke="currentColor" fill="none" opacity=".6" />
        <polyline points={curve.map(([t,r])=>`${x(t)},${y(r)}`).join(" ")} stroke={colors.accent} strokeWidth={2.5*L.scale} fill="none" />
        {curve.map(([t,r],i)=><g key={i} onPointerDown={event=>{select(i);grab(event,i);}}>
          <circle data-fan-point={i+1} cx={x(t)} cy={y(r)} r={fullscreen ? 22*L.scale : 16} fill="transparent" />
          <circle cx={x(t)} cy={y(r)} r={(i===selected ? (fullscreen ? 8 : 7) : (fullscreen ? 5 : 4))*L.scale} fill={i===selected ? colors.curve.selected : colors.accent}
            stroke={colors.accent} strokeWidth={2*L.scale} /></g>)}
        {fresh && <g data-fan-live-marker="true" style={{pointerEvents:"none"}}><circle cx={x(Math.min(100,Math.max(0,status.readings!.apu_temperature!)))}
          cy={y(Math.min(max,Math.max(0,status.readings!.rpm!)))} r={6*L.scale} fill={colors.curve.live} stroke={colors.surface} strokeWidth={2*L.scale} />
          <text x={left} y={font*1.5} fill={colors.curve.live} fontSize={font}>{`Live tach · ${fanReadingLabel(status)}`}</text></g>}
        <text x={left} y={L.height-font*0.6} fill="currentColor" fontSize={font}>{pointText(selected,curve.length,curve[selected][0],speedLabel(curve[selected][1],status))}</text>
      </>;
    }}
  </Graph>;
};

type EditorDraft = {curve: Curve; dirty: boolean; revision: number};
type EditorSnapshot = {scope: string; selected: number; drafts: Record<string, EditorDraft>; rpmStep: number};
// The dialog's body is the page's full-screen layout, with its own reads while the QAM page waits behind it.
const FullEditor: FC<{initial: EditorSnapshot; onResult: (result: EditorSnapshot) => void; onClosed: () => void; closeModal?: () => void}> =
  ({initial,onResult,onClosed,closeModal}) => <CurveDialog title="Fan curve editor" closeModal={closeModal}>
    <div style={{height:"100%",flex:1,width:"100%",minHeight:0,display:"flex",flexDirection:"column",gap:12}} data-fan-fullscreen="true">
      <FanPage fullscreen initialEditor={initial} onEditorChange={onResult} onClose={() => {closeModal?.();onClosed();}} />
    </div>
  </CurveDialog>;

export const FanPage: FC<{viewVisible?: boolean; isViewVisible?: () => boolean; fullscreen?: boolean;
  initialEditor?: EditorSnapshot; onEditorChange?: (snapshot: EditorSnapshot) => void; onClose?: () => void;
  /** Leaving the QAM page asks first while any curve has unsaved changes (index.tsx). */
  guardLeave?: (check: (() => string | null) | null) => void}> =
  ({viewVisible=true,isViewVisible=() => true,fullscreen=false,initialEditor,onEditorChange,onClose,guardLeave} = {}) => {
  const qamVisible = useQuickAccessVisible();
  const [modalOpen, setModalOpen] = useState(false);
  const pageRoot = useRef<HTMLDivElement | null>(null);
  const [onScreen, setOnScreen] = useState(true);
  const rootVisible = () => !pageRoot.current || !pageRoot.current.ownerDocument.hidden && pageRoot.current.getClientRects().length > 0;
  const visible = (fullscreen || qamVisible && viewVisible) && !modalOpen && onScreen;
  useEffect(() => {
    const element=pageRoot.current;if(!element)return;
    const doc=element.ownerDocument;const Observer=doc.defaultView?.IntersectionObserver;
    const update=()=>setOnScreen(rootVisible());doc.addEventListener("visibilitychange",update);update();
    const observer=Observer ? new Observer(update) : null;observer?.observe(element);
    return()=>{doc.removeEventListener("visibilitychange",update);observer?.disconnect();};
  }, []);
  const [status, setStatus] = useState<FanState | null>(null);
  const [scope, setScope] = useState(initialEditor?.scope ?? "global");
  const [draft, setDraft] = useState<Curve | null>(null);
  const draftRef = useRef(draft); draftRef.current = draft;
  const [selected, setSelected] = useState(initialEditor?.selected ?? 0);
  const [rpmStep, setRpmStep] = useState(initialEditor?.rpmStep ?? 100);
  const drafts = useRef<Record<string,EditorDraft>>(initialEditor?.drafts ?? {});
  const modal = useRef<ReturnType<typeof showModal> | null>(null);
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  // The control whose last change failed shows that error until the next change.
  const [failed, setFailed] = useState<{control: string; text: string} | null>(null);
  const [readError, setReadError] = useState("");
  const startingSince = useRef<number | null>(null);
  const [readingNow, setReadingNow] = useState(Date.now());
  const dirtyRef = useRef(false), mounted = useRef(false), baseRevision = useRef(0);
  const mutationEpoch = useRef(0), busyRef = useRef(false), acceptedRevision = useRef(-1);
  const scopeRef = useRef(scope);
  scopeRef.current = scope; dirtyRef.current = dirty;
  const accept = (next: FanState) => {
    if (!mounted.current) return;
    if (next.settings.revision < acceptedRevision.current) return;
    acceptedRevision.current = next.settings.revision;
    setReadError("");
    setStatus(next);
    if (!next.settings.assign_tdp_presets && scopeRef.current !== "global") {
      if (draftRef.current) drafts.current[scopeRef.current]={curve:clone(draftRef.current),dirty:dirtyRef.current,revision:baseRevision.current};
      scopeRef.current="global";setScope("global");setSelected(0);dirtyRef.current=false;setDirty(false);
    }
    if (!dirtyRef.current) {
      const stored = drafts.current[scopeRef.current];
      if (stored?.dirty) {setDraft(clone(stored.curve));baseRevision.current=stored.revision;dirtyRef.current=true;setDirty(true);}
      else {setDraft(clone(next.settings.presets[scopeRef.current] ?? next.settings.global_curve));baseRevision.current=next.settings.revision;}
    }
  };
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; modal.current?.Close();modal.current=null; }; }, []);
  useEffect(() => {
    guardLeave?.(() => dirtyRef.current || Object.values(drafts.current).some(entry => entry.dirty)
      ? "Your unsaved curve changes are lost. The saved curves stay." : null);
    return () => guardLeave?.(null);
  }, []);
  useEffect(() => {
    if (!draft) return;
    drafts.current[scope]={curve:clone(draft),dirty,revision:baseRevision.current};
    onEditorChange?.({scope,selected,drafts:JSON.parse(JSON.stringify(drafts.current)),rpmStep});
  }, [draft,dirty,scope,selected,rpmStep]);
  useEffect(() => {
    if (!visible) return;
    setReadingNow(Date.now());
    // Expire numbers even if an RPC is stuck; this timer performs no I/O.
    const timer = setInterval(() => setReadingNow(Date.now()), 250);
    return () => clearInterval(timer);
  }, [visible]);
  useEffect(() => {
    if (!visible) return;
    let cancelled = false, timer: ReturnType<typeof setTimeout> | undefined;
    const poll = async () => {
      if (cancelled) return;
      const epoch = mutationEpoch.current;
      try { const next = await getFanState(() => !cancelled && rootVisible() && (fullscreen || isViewVisible())); if (!cancelled && epoch === mutationEpoch.current) accept(next); }
      catch (e) { if (!cancelled && epoch === mutationEpoch.current) {
        setReadError(errorText(e));
        setStatus(previous => previous && { ...previous, readings: { valid: false, error: errorText(e) } });
      } }
      finally { if (!cancelled) timer = setTimeout(() => void poll(), 1000); }
    };
    void poll();
    return () => { cancelled = true; if (timer !== undefined) clearTimeout(timer); };
  }, [visible]);
  const mutate = async (operation: () => Promise<FanState>, control: string, saved=false) => {
    if (busyRef.current) return;
    busyRef.current = true; mutationEpoch.current += 1;
    setBusy(true); setFailed(null);
    const committedRevision = saved ? baseRevision.current : undefined;
    try {
      const next = await ordered(operation);
      if (mounted.current) {
        if (saved) {
          for (const entry of Object.values(drafts.current)) {
            if (entry.revision === committedRevision) entry.revision = next.settings.revision;
          }
          dirtyRef.current = false; setDirty(false); delete drafts.current[scopeRef.current];
        }
        accept(next);
        if (saved) notify("Curve saved.");
      }
    } catch (e) {
      if (mounted.current) { setFailed({control, text: errorText(e)}); notify(errorText(e)); }
    }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  const failure = (control: string) => failed?.control === control ? failed.text : undefined;
  if (!status || !draft) return <div ref={pageRoot}>{fullscreen ? <DialogBody>{readError || "Reading fan…"}</DialogBody>
    // The page reads again every second by itself: a failed first read shows why, without a button.
    : <PanelSection>{readError ? infoRow("Fan Control unavailable", readError) : <PanelSectionRow><Spinner /></PanelSectionRow>}</PanelSection>}</div>;
  const limits = status.limits;
  const edit = (axis: 0 | 1, value: number, point=selected) => {
    if (busyRef.current || point===7 && axis===0) return;
    setDraft(previous => previous && editPoint(previous,point,axis,value,limits));dirtyRef.current=true;setDirty(true);
  };
  const choose = (next: string) => {
    if (busyRef.current || next!=="global" && !status.settings.assign_tdp_presets) return;
    drafts.current[scope]={curve:clone(draft),dirty:dirtyRef.current,revision:baseRevision.current};
    setScope(next); scopeRef.current = next; setSelected(0);
    const stored=drafts.current[next];const changed=!!stored?.dirty;
    setDirty(changed);dirtyRef.current=changed;
    setDraft(clone(changed ? stored.curve : status.settings.presets[next] ?? status.settings.global_curve));
    baseRevision.current=changed ? stored.revision : status.settings.revision;setFailed(null);
  };
  const discard = () => {delete drafts.current[scope];dirtyRef.current=false;setDirty(false);
    setDraft(clone(status.settings.presets[scope] ?? status.settings.global_curve));baseRevision.current=status.settings.revision;};
  const unit = status.settings.display_unit ?? "rpm";
  const assignmentMode = !!status.settings.assign_tdp_presets;
  const matchingTemplate=Object.entries(status.templates ?? {}).find(([,points])=>points.length===draft.length && points.every(([t,r],i)=>t===draft[i][0] && r===draft[i][1]))?.[0] ?? "";
  const templateOptions=[...(!matchingTemplate ? [{data:"",label:"Edited curve"}] : []),...Object.keys(status.templates ?? {}).map(name=>({data:name,label:title(name)}))];
  const applyTemplate = (name: string) => {
    const points=status.templates?.[name];if(busyRef.current || !points)return;
    setDraft(clone(points));setSelected(0);dirtyRef.current=true;setDirty(true);
    notify(`${title(name)} loaded. Save to use it.`);
  };
  const displayedStep = speedEditStep(rpmStep,status);
  // Drafts based on `expected` stay saveable after this UI's own settings-only change.
  const rebase = (expected: number, revision: number) => {
    if(baseRevision.current===expected)baseRevision.current=revision;
    for(const entry of Object.values(drafts.current))if(entry.revision===expected)entry.revision=revision;
  };
  const switchUnit = (percent: boolean) => {
    const expected=status.settings.revision;
    void mutate(async()=>{const next=await saveUnit(percent ? "percent" : "rpm",expected);rebase(expected,next.settings.revision);return next;},"unit");
  };
  const switchAssignments = (enabled: boolean) => {
    const expected=status.settings.revision;
    void mutate(async()=>{const next=await saveAssignments(enabled,expected);rebase(expected,next.settings.revision);return next;},"assign");
  };
  const curvesOf = (state: FanState) => JSON.stringify([state.settings.global_curve,state.settings.presets]);
  const toggle = (enabled: boolean) => {
    const expected=status.settings.revision, curves=curvesOf(status);
    void mutate(async()=>{const next=await setEnabled(enabled);
      // fan_set_enabled has no expected revision: rebase only when its own +1 is the sole change.
      if(next.settings.revision===expected+1 && curvesOf(next)===curves)rebase(expected,next.settings.revision);
      return next;},"enabled");
  };
  const openFull = () => {
    if (busyRef.current || fullscreen) return;
    drafts.current[scope]={curve:clone(draft),dirty:dirtyRef.current,revision:baseRevision.current};
    const initial={scope,selected,drafts:JSON.parse(JSON.stringify(drafts.current)),rpmStep};
    setModalOpen(true);
    modal.current=showModal(<FullEditor initial={initial} onClosed={() => setModalOpen(false)}
      onResult={result=>{if(!mounted.current)return;drafts.current=result.drafts;setScope(result.scope);scopeRef.current=result.scope;
        setSelected(result.selected);setRpmStep(result.rpmStep);const entry=result.drafts[result.scope];
        if(entry){setDraft(clone(entry.curve));setDirty(entry.dirty);dirtyRef.current=entry.dirty;baseRevision.current=entry.revision;}}} />,
      undefined,{strTitle:"Fan curve editor",bNeverPopOut:true,fnOnClose:()=>{modal.current=null;if(mounted.current)setModalOpen(false);}});
  };
  const curveOptions=["global",...status.presets].map(p=>({data:p,label:`${title(p)}${p!=="global" && !status.settings.presets[p] ? " · inherits global" : ""}${drafts.current[p]?.dirty ? " · unsaved" : ""}`}));
  const stepOptions=(unit === "percent" ? PERCENT_EDIT_STEPS : RPM_EDIT_STEPS).map(step=>({data:step,label:unit === "percent" ? `${step}%` : `${step} RPM`}));
  const graph=<CurveGraph curve={draft} selected={selected} status={status} disabled={busy} fullscreen={fullscreen}
    rpmStep={unit === "percent" ? displayedStep*limits.max_rpm/100 : displayedStep} select={setSelected} edit={edit} />;
  const saveDisabled=busy || (!dirty && !status.needs_curve_review && (scope==='global' || !!status.settings.presets[scope]));
  const save=()=>void mutate(()=>saveCurve(scope,clone(draft),baseRevision.current),"save",true);
  const useGlobal=()=>void mutate(()=>saveCurve(scope,null,baseRevision.current),"save",true);
  const now=Math.max(readingNow,Date.now());
  // Time spent in "Starting your curve…", for the stuck hint after 30 s.
  const starting=status.supported && status.settings.enabled && !status.error && !status.telemetry?.active;
  if(!starting)startingSince.current=null;else if(startingSince.current===null)startingSince.current=now;
  const fanState=fanStatus(status,starting ? now-(startingSince.current ?? now) : 0);
  const enableDescription=failure("enabled") ?? (!status.settings.enabled && (dirty || status.needs_curve_review)
    ? (status.needs_curve_review ? "Review and save the curve first." : "Save or discard your curve changes first.")
    : fanState.detail);
  if(fullscreen) return <div ref={pageRoot} data-fan-page="true" data-fan-dialog-body="true" style={{minHeight:0,flex:1,display:"flex",flexDirection:"column",gap:12}}>
    <DialogBody style={{minHeight:0,flex:1,overflowY:"auto",margin:0,padding:0,display:"flex",flexDirection:"column",gap:8}}>
      <div data-fan-dialog-status="true">{fanReadingLabel(status,now)} · {fanState.headline}</div>
      <Focusable flow-children="row" data-fan-controls="true" style={{display:"grid",gridTemplateColumns:`repeat(${assignmentMode ? 4 : 3},minmax(0,1fr))`,gap:16,margin:0,flexShrink:0}}>
        {assignmentMode && <div><DialogLabel>Curve</DialogLabel><Dropdown menuLabel="Curve" selectedOption={scope} rgOptions={curveOptions} disabled={busy} onChange={option=>choose(option.data)} /></div>}
        <div><DialogLabel>Starting curve</DialogLabel><Dropdown menuLabel="Starting curve" selectedOption={matchingTemplate} rgOptions={templateOptions} disabled={busy} onChange={option=>applyTemplate(option.data)} /></div>
        <div><DialogLabel>Speed unit</DialogLabel><Dropdown menuLabel="Speed unit" selectedOption={unit} rgOptions={[{data:"rpm",label:"RPM"},{data:"percent",label:"Percent"}]} disabled={busy} onChange={option=>switchUnit(option.data==="percent")} /></div>
        <div><DialogLabel>Speed edit step</DialogLabel><Dropdown menuLabel="Speed edit step" selectedOption={displayedStep} rgOptions={stepOptions} disabled={busy} onChange={option=>setRpmStep(unit==="percent" ? Math.round(option.data*limits.max_rpm/100) : option.data)} /></div>
      </Focusable>
      {assignmentMode && <div data-fan-assignment-note="true" style={{color:colors.accent,fontSize:13}}>{ASSIGNMENT_NOTE}</div>}
      <div data-fan-dialog-graph="true" style={{minHeight:260,flex:"1 0 260px",maxHeight:480}}>{graph}</div>
      {failed && <div role="status">{failed.text}</div>}
      {scope!=="global" && status.settings.presets[scope] && <DialogButton disabled={busy || dirty} onClick={useGlobal}>Use global curve for {title(scope)}</DialogButton>}
      <div style={{fontSize:13,color:colors.muted}}>8 points · the 100°C point can't go below {speedLabel(limits.last_min_rpm,status)}{dirty ? " · Unsaved changes" : ""}</div>
    </DialogBody>
    <DialogFooter style={{display:"flex",gap:12,margin:0,padding:0,flexShrink:0}}>
      <Focusable flow-children="row" data-fan-footer="true" style={{display:"flex",gap:12,width:"100%"}}>
      <DialogButton style={{flex:1}} disabled={saveDisabled} onClick={save}>{saveLabel(scope)}</DialogButton>
      <DialogButton style={{flex:1}} disabled={busy || !dirty} onClick={discard}>Discard changes</DialogButton>
      <DialogButton style={{flex:1}} onClick={onClose}>Back to Fan Control</DialogButton>
      </Focusable>
    </DialogFooter>
  </div>;
  return <div ref={pageRoot} data-fan-page="true">
    <PanelSection>
      <div data-fan-summary="true" style={{textAlign:"center",padding:"4px 0 12px"}}>
        <div style={{fontSize:20,fontWeight:600}}>{fanReadingLabel(status,now)}</div>
        <div style={{fontSize:13,color:colors.muted,marginTop:4}}>{fanState.headline}</div>
      </div>
      {status.beta && infoRow("Beta", FAN_BETA_TEXT)}
      <PanelSectionRow><ToggleField label="Enable fan control" checked={status.settings.enabled}
        disabled={busy || !status.settings.enabled && (!status.supported || !!status.ownership?.blocked || dirty || !!status.needs_curve_review)}
        description={enableDescription}
        onChange={toggle} /></PanelSectionRow>
    </PanelSection>
    <PanelSection title={dirty ? "Curve · unsaved" : undefined}>
      {assignmentMode && <PanelSectionRow><DropdownItem label="Curve" selectedOption={scope} disabled={busy}
        rgOptions={curveOptions} onChange={option=>choose(option.data)} /></PanelSectionRow>}
      <PanelSectionRow><DropdownItem label="Starting curve" selectedOption={matchingTemplate} disabled={busy}
        rgOptions={templateOptions} onChange={option=>applyTemplate(option.data)} /></PanelSectionRow>
      <PanelSectionRow>{graph}</PanelSectionRow>
      <PanelSectionRow><DropdownItem label="Speed edit step" selectedOption={displayedStep} disabled={busy}
        rgOptions={stepOptions}
        onChange={option=>setRpmStep(unit === "percent" ? Math.round(option.data*limits.max_rpm/100) : option.data)} /></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={openFull}>Full screen curve editor</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={saveDisabled} onClick={save} description={failure("save")}>
        {saveLabel(scope)}</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || !dirty} onClick={discard}>Discard changes</ButtonItem></PanelSectionRow>
      {scope !== "global" && status.settings.presets[scope] && <PanelSectionRow><ButtonItem layout="below" disabled={busy || dirty}
        onClick={useGlobal}>Use global curve for {title(scope)}</ButtonItem></PanelSectionRow>}
    </PanelSection>
    <PanelSection title="Settings">
      <PanelSectionRow><ToggleField label="Assign curves to TDP presets" checked={assignmentMode} disabled={busy || dirty || !!status.needs_curve_review}
        description={failure("assign") ?? <span data-fan-assignment-note="true">{ASSIGNMENT_NOTE}</span>} onChange={switchAssignments} /></PanelSectionRow>
      {/* The page's last row is never disabled; mutate() ignores it while another change runs. */}
      <PanelSectionRow><ToggleField label="Show fan speed as percentage" checked={unit === "percent"}
        description={failure("unit") ?? "RPM or percent in every fan view."} onChange={switchUnit} /></PanelSectionRow>
    </PanelSection>
  </div>;
};
