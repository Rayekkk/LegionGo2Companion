// SPDX-License-Identifier: BSD-3-Clause
import { callable, useQuickAccessVisible } from "@decky/api";
import { ButtonItem, Field, PanelSection, PanelSectionRow, ToggleField, Focusable, GamepadButton,
  DropdownItem, Dropdown, DialogHeader, DialogBody, DialogLabel,
  DialogFooter, DialogButton, ModalRoot, showModal, type GamepadEvent } from "@decky/ui";
import { FC, useEffect, useRef, useState } from "react";

export type Curve = [number, number][];
export interface FanState {
  supported: boolean; error: string; active_curve: string;
  needs_curve_review?: boolean;
  settings: { enabled: boolean; revision: number; assign_tdp_presets?: boolean; display_unit?: "rpm" | "percent"; global_curve: Curve; presets: Record<string, Curve> };
  templates?: Record<string, Curve>;
  limits: { min_rpm: number; max_rpm: number; step: number; hard_max: number };
  ownership?: {blocked: boolean; state: string; reason: string};
  presets: string[];
  readings?: { valid: boolean; rpm?: number; apu_temperature?: number; age_seconds?: number;
    automatic?: boolean; source?: string; error: string };
  receivedAt?: number;
  telemetry?: { active: boolean; automatic: boolean; error?: string; rpm?: number; target?: number;
    override?: number; effective?: number; stop_verified?: boolean; temperatures?: Record<string, number> };
}
const readState = callable<[], FanState>("fan_get_state");
const requestEnabled = callable<[boolean], FanState>("fan_set_enabled");
const requestCurve = callable<[string, Curve | null, number], FanState>("fan_save_curve");
const requestUnit = callable<["rpm" | "percent", number], FanState>("fan_save_unit");
const requestAssignments = callable<[boolean, number], FanState>("fan_save_assignment_mode");
const timed = async (operation: () => Promise<FanState>) => {
  const started = Date.now(), next = await operation(), receivedAt = Date.now();
  return { ...next, receivedAt, readings: next.readings && { ...next.readings,
    age_seconds: (next.readings.age_seconds ?? 0) + Math.max(0, receivedAt-started)/1000 } };
};
const setEnabled = (enabled: boolean) => timed(() => requestEnabled(enabled));
const saveCurve = (scope: string, curve: Curve | null, revision: number) => timed(() => requestCurve(scope, curve, revision));
const saveUnit = (unit: "rpm" | "percent", revision: number) => timed(() => requestUnit(unit, revision));
const saveAssignments = (enabled: boolean, revision: number) => timed(() => requestAssignments(enabled, revision));
const ASSIGNMENT_NOTE = "Applies to Companion TDP presets, not SteamOS QAM Performance profiles.";
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
const title = (name: string) => name === "max" ? "Maximum" : name[0].toUpperCase() + name.slice(1);
export const speedLabel = (rpm: number | undefined, state: Pick<FanState, "settings" | "limits">) => {
  const percent = state.settings.display_unit === "percent";
  if (rpm === undefined || !Number.isFinite(rpm)) return percent ? "—%" : "— RPM";
  return percent ? `${Math.min(100, Math.max(0, rpm/state.limits.max_rpm*100)).toFixed(1)}%` : `${rpm} RPM`;
};
const RPM_EDIT_STEPS = [1,10,50,100,200,500];
const PERCENT_EDIT_STEPS = [1,2,5,10];
export const speedEditStep = (rpmStep: number, state: Pick<FanState, "settings" | "limits">) => {
  const percent = state.settings.display_unit === "percent";
  const options = percent ? PERCENT_EDIT_STEPS : RPM_EDIT_STEPS;
  const wanted = percent ? rpmStep/state.limits.max_rpm*100 : rpmStep;
  return options.reduce((best, value) => Math.abs(value-wanted) < Math.abs(best-wanted) ? value : best);
};
export const fanReadingLabel = (state?: FanState, now = Date.now()) => {
  const r = state?.readings;
  const age = (r?.age_seconds ?? Infinity) + (now-(state?.receivedAt ?? -Infinity))/1000;
  if (!r?.valid || !Number.isFinite(age) || age < 0 || age > 2.5 ||
      !Number.isFinite(r.apu_temperature) || !Number.isInteger(r.rpm)) return `APU — · ${state ? speedLabel(undefined, state) : "— RPM"}`;
  return `APU ${r.apu_temperature!.toFixed(1)}°C · ${speedLabel(r.rpm, state!)}`;
};
export const fanMode = (state?: FanState) => !state ? "Reading fan state…"
  : state.error ? "Fan Control needs attention" : !state.supported ? "Fan Control unavailable"
  : !state.settings.enabled && state.ownership?.blocked ? "Fan Control blocked"
  : state.settings.enabled ? `${title(state.active_curve)} custom curve`
  : state.readings?.valid && state.readings.automatic === false ? "External fan control"
  : state.telemetry && !state.telemetry.automatic ? "Fan recovery pending" : "Firmware automatic";
export const fanSummary = (state?: FanState) => `${fanReadingLabel(state)} · ${fanMode(state)}`;

export function editPoint(curve: Curve, index: number, axis: 0 | 1, value: number, limits: FanState["limits"]): Curve {
  const next = clone(curve);
  if (!Number.isFinite(value) || index < 0 || index >= curve.length-1 || !Number.isInteger(index)) return next;
  if (axis === 0) {
    next[index][0] = Math.max(index ? curve[index-1][0]+1 : 0, Math.min(curve[index+1][0]-1, Math.round(value)));
  } else {
    let rpm = Math.round(value/limits.step)*limits.step;
    if (rpm > 0 && rpm < limits.min_rpm) rpm = value > curve[index][1] ? limits.min_rpm : 0;
    next[index][1] = Math.max(index ? curve[index-1][1] : 0, Math.min(curve[index+1][1], limits.max_rpm, rpm));
  }
  return next;
}

export const graphLayout = (fullscreen: boolean, width=800, height=308) => {
  if(!fullscreen)return {width:400,height:248,left:76,span:292,bottom:202,rise:170,font:12,scale:1};
  const viewWidth=Math.max(800,Math.round(width/height*308)),scale=viewWidth/width;
  return {width:viewWidth,height:308,left:Math.round(viewWidth*.14),span:Math.round(viewWidth*.8),bottom:244,rise:200,font:Math.max(18,14*scale),scale};
};
export const CurveGraph: FC<{curve: Curve; selected: number; status: FanState; disabled: boolean; rpmStep: number;
  fullscreen?: boolean;
  select: (point: number) => void; edit: (axis: 0|1, value: number, point?: number) => void}> = ({curve, selected, status, disabled, rpmStep, select, edit, fullscreen=false}) => {
  const [steering, setSteering] = useState(false);
  const drag = useRef<{point: number; pointer: number} | null>(null);
  const svg = useRef<SVGSVGElement | null>(null);
  const [frame,setFrame]=useState({width:800,height:308});
  useEffect(()=>{
    const element=svg.current;if(!fullscreen || !element)return;
    const update=()=>{const r=element.getBoundingClientRect();if(r.width>0 && r.height>0)setFrame(previous=>Math.abs(previous.width-r.width)<.5 && Math.abs(previous.height-r.height)<.5 ? previous : {width:r.width,height:r.height});};
    const Observer=element.ownerDocument.defaultView?.ResizeObserver;const observer=Observer ? new Observer(update) : null;
    observer?.observe(element);update();return()=>observer?.disconnect();
  },[fullscreen]);
  const layout=graphLayout(fullscreen,frame.width,frame.height);
  const {left,span,bottom,rise,font}=layout;
  const x = (t: number) => left+t*span/100, y = (r: number) => bottom-r/status.limits.max_rpm*rise;
  const consume = (event: Event) => {event.preventDefault();event.stopPropagation();};
  const enter = (event: Event) => {if(disabled)return;consume(event);setSteering(true);};
  const leave = (event: Event) => {consume(event);setSteering(false);};
  const handleBumper = (event: GamepadEvent) => {
    const button = event.detail.button;
    if (button === GamepadButton.BUMPER_LEFT || button === GamepadButton.BUMPER_RIGHT) {
      event.preventDefault();event.stopPropagation(); select((selected+(button === GamepadButton.BUMPER_LEFT ? 7 : 1))%8); return;
    }
  };
  const handleDirection = (event: GamepadEvent) => {
    const button = event.detail.button;
    if (!steering || disabled) return;
    if ([GamepadButton.DIR_LEFT,GamepadButton.DIR_RIGHT,GamepadButton.DIR_UP,GamepadButton.DIR_DOWN].includes(button)) {
      event.preventDefault();event.stopPropagation();
      if (button === GamepadButton.DIR_LEFT || button === GamepadButton.DIR_RIGHT) edit(0, curve[selected][0]+(button === GamepadButton.DIR_LEFT ? -1 : 1));
      else edit(1, curve[selected][1]+(button === GamepadButton.DIR_UP ? rpmStep : -rpmStep));
    }
  };
  const move = (event: React.PointerEvent<SVGSVGElement>) => {
    if (disabled || !drag.current || drag.current.pointer !== event.pointerId || !svg.current) return;
    const matrix = svg.current.getScreenCTM(); if (!matrix) return;
    const local = svg.current.createSVGPoint();local.x=event.clientX;local.y=event.clientY;
    const point = local.matrixTransform(matrix.inverse());
    edit(0,(point.x-left)*100/span,drag.current.point);edit(1, Math.round((bottom-point.y)/rise*status.limits.max_rpm/rpmStep)*rpmStep,drag.current.point);
  };
  const fresh = !fanReadingLabel(status).includes("—");
  return <Focusable data-fan-editor="true" data-fan-editing={steering ? "true" : "false"} tabIndex={0}
    focusClassName="lg2-fan-graph-focused" onActivate={enter} onOKButton={enter}
    onCancelButton={steering ? leave : undefined} onButtonDown={steering ? handleBumper : undefined}
    onGamepadDirection={steering ? handleDirection : undefined} onGamepadBlur={() => setSteering(false)}
    onOKActionDescription={steering ? undefined : "Edit curve"} onCancelActionDescription={steering ? "Finish editing" : undefined}
    actionDescriptionMap={steering ? {[GamepadButton.BUMPER_LEFT]:"Previous point",[GamepadButton.BUMPER_RIGHT]:"Next point"} : {}}
    style={{border:steering ? "2px solid #66c0f4" : "2px solid #52606d", borderRadius:8,width:"100%",boxSizing:"border-box",...(fullscreen ? {height:"100%"} : {})}}>
    <style>{`.lg2-fan-graph-focused{outline:2px solid #fff;outline-offset:2px}`}</style>
    <div data-fan-controller-hint="true" style={{padding:"6px 10px",fontSize:fullscreen ? 16 : 12,color:steering ? "#66c0f4" : "#dcdedf"}}>
      {steering ? "Editing · LB/RB point · ←/→ 1°C · ↑/↓ speed · B finish" : "Focus graph and press A to edit with controller"}
    </div>
    <svg ref={svg} data-fan-graph="true" data-fan-graph-layout={fullscreen ? "dialog" : "qam"} role="img" aria-label="Eight-point fan curve. Drag points or press A and use D-pad. LB/RB selects a point. Point eight is fixed at 100°C and full speed."
      viewBox={`0 0 ${layout.width} ${layout.height}`} style={{width:"100%",display:"block",touchAction:"none",...(fullscreen ? {height:"calc(100% - 36px)"} : {})}}
      onPointerMove={move} onPointerUp={event => {drag.current=null;event.currentTarget.releasePointerCapture?.(event.pointerId);}}
      onPointerCancel={() => {drag.current=null;}} onLostPointerCapture={() => {drag.current=null;}}>
      <rect x={left} y={y(status.limits.min_rpm)} width={span} height={bottom-y(status.limits.min_rpm)} fill="currentColor" opacity=".05" />
      {[0,status.limits.min_rpm,status.limits.max_rpm].map(r => <g key={r}>
        <path d={`M${left} ${y(r)}H${left+span}`} stroke="currentColor" opacity=".2" />
        <text x={left-7} y={y(r)+4} textAnchor="end" fill="currentColor" fontSize={font}>{speedLabel(r,status)}</text>
      </g>)}
      {[0,20,40,60,80,100].map(t => <text key={t} x={x(t)} y={bottom+22} textAnchor="middle" fill="currentColor" fontSize={font}>{t}°</text>)}
      <path d={`M${left} ${bottom-rise}V${bottom}H${left+span}`} stroke="currentColor" fill="none" opacity=".6" />
      <polyline points={curve.map(([t,r])=>`${x(t)},${y(r)}`).join(" ")} stroke="#66c0f4" strokeWidth={fullscreen ? 2.5*layout.scale : 2.5} fill="none" />
      {curve.map(([t,r],i)=><g key={i} onPointerDown={event=>{
        event.preventDefault();select(i);if(!disabled && i<7){drag.current={point:i,pointer:event.pointerId};svg.current?.setPointerCapture(event.pointerId);}
      }}><circle data-fan-point={i+1} cx={x(t)} cy={y(r)} r={fullscreen ? 22*layout.scale : 16} fill="transparent" />
        <circle cx={x(t)} cy={y(r)} r={fullscreen ? (i===selected ? 8 : 5)*layout.scale : i===selected ? 7 : 4} fill={i===selected ? "#fff" : "#66c0f4"} stroke="#66c0f4" strokeWidth={fullscreen ? 2*layout.scale : 2} /></g>)}
      {fresh && <g data-fan-live-marker="true"><circle cx={x(Math.min(100,Math.max(0,status.readings!.apu_temperature!)))}
        cy={y(Math.min(status.limits.max_rpm,Math.max(0,status.readings!.rpm!)))} r="6" fill="#f5c96b" stroke="#111" strokeWidth="2" />
        <text x={left} y={fullscreen ? 24 : 18} fill="#f5c96b" fontSize={font}>{`Live tach · ${fanReadingLabel(status)}`}</text></g>}
      <text x={left} y={fullscreen ? 299 : 240} fill="currentColor" fontSize={font}>{`Point ${selected+1}/8 · ${curve[selected][0]}°C · ${speedLabel(curve[selected][1],status)}${selected===7 ? " · fixed" : ""}`}</text>
    </svg>
  </Focusable>;
};

type EditorDraft = {curve: Curve; dirty: boolean; revision: number};
type EditorSnapshot = {scope: string; selected: number; drafts: Record<string, EditorDraft>; rpmStep: number};
const FullEditor: FC<{initial: EditorSnapshot; onResult: (result: EditorSnapshot) => void; onClosed: () => void; closeModal?: () => void}> =
  ({initial,onResult,onClosed,closeModal}) => <ModalRoot className="lg2-fan-dialog" modalClassName="lg2-fan-dialog-layer" bAllowFullSize onCancel={closeModal} closeModal={closeModal}>
    <style>{`.lg2-fan-dialog{width:min(1120px,94vw)!important;max-width:94vw!important;height:100%!important;max-height:900px!important;min-height:0!important;display:flex!important;flex-direction:column!important;flex:1 1 auto!important;overflow:hidden!important;box-sizing:border-box!important;padding:20px!important}.lg2-fan-dialog-layer{padding:16px!important;overflow:hidden!important;align-items:center!important;justify-content:center!important}.lg2-fan-dialog button{min-height:40px}.lg2-fan-dialog *{box-sizing:border-box}`}</style>
    <div style={{height:"100%",flex:1,width:"100%",minHeight:0,display:"flex",flexDirection:"column",gap:12}} data-fan-fullscreen="true">
      <DialogHeader style={{margin:0,flexShrink:0}}>Fan curve editor</DialogHeader>
      <FanPage fullscreen initialEditor={initial} onEditorChange={onResult} onClose={() => {closeModal?.();onClosed();}} />
    </div>
  </ModalRoot>;

export const FanPage: FC<{viewVisible?: boolean; isViewVisible?: () => boolean; fullscreen?: boolean;
  initialEditor?: EditorSnapshot; onEditorChange?: (snapshot: EditorSnapshot) => void; onClose?: () => void}> =
  ({viewVisible=true,isViewVisible=() => true,fullscreen=false,initialEditor,onEditorChange,onClose} = {}) => {
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
  const [notice, setNotice] = useState("");
  const [readingNow, setReadingNow] = useState(Date.now());
  const dirtyRef = useRef(false), mounted = useRef(false), baseRevision = useRef(0);
  const mutationEpoch = useRef(0), busyRef = useRef(false), acceptedRevision = useRef(-1);
  const scopeRef = useRef(scope);
  scopeRef.current = scope; dirtyRef.current = dirty;
  const accept = (next: FanState) => {
    if (!mounted.current) return;
    if (next.settings.revision < acceptedRevision.current) return;
    acceptedRevision.current = next.settings.revision;
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
        setNotice(String(e));
        setStatus(previous => previous && { ...previous, readings: { valid: false, error: String(e) } });
      } }
      finally { if (!cancelled) timer = setTimeout(() => void poll(), 1000); }
    };
    void poll();
    return () => { cancelled = true; if (timer !== undefined) clearTimeout(timer); };
  }, [visible]);
  const mutate = async (operation: () => Promise<FanState>, saved=false) => {
    if (busyRef.current) return;
    busyRef.current = true; mutationEpoch.current += 1;
    setBusy(true); setNotice("");
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
        accept(next); setNotice(saved ? "Curve saved and read back." : "Fan state confirmed.");
      }
    } catch (e) { if (mounted.current) setNotice(String(e)); }
    finally { busyRef.current = false; if (mounted.current) setBusy(false); }
  };
  if (!status || !draft) return <div ref={pageRoot}>{fullscreen ? <DialogBody>{notice || "Reading fan state…"}</DialogBody>
    : <PanelSection><PanelSectionRow><Field focusable label="Fan Control" description={notice || "Reading fan state…"} /></PanelSectionRow></PanelSection>}</div>;
  const telemetry = status.telemetry, limits = status.limits;
  const edit = (axis: 0 | 1, value: number, point=selected) => {
    if (busyRef.current || point===7) return;
    setDraft(previous => previous && editPoint(previous,point,axis,value,limits));dirtyRef.current=true;setDirty(true);
  };
  const choose = (next: string) => {
    if (busyRef.current || next!=="global" && !status.settings.assign_tdp_presets) return;
    drafts.current[scope]={curve:clone(draft),dirty:dirtyRef.current,revision:baseRevision.current};
    setScope(next); scopeRef.current = next; setSelected(0);
    const stored=drafts.current[next];const changed=!!stored?.dirty;
    setDirty(changed);dirtyRef.current=changed;
    setDraft(clone(changed ? stored.curve : status.settings.presets[next] ?? status.settings.global_curve));
    baseRevision.current=changed ? stored.revision : status.settings.revision;setNotice("");
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
    setNotice(`${title(name)} starting curve loaded. Edit or save to apply.`);
  };
  const displayedStep = speedEditStep(rpmStep,status);
  const switchUnit = (percent: boolean) => {
    const expected=status.settings.revision;
    void mutate(async()=>{const next=await saveUnit(percent ? "percent" : "rpm",expected);
      if(baseRevision.current===expected)baseRevision.current=next.settings.revision;
      for(const entry of Object.values(drafts.current))if(entry.revision===expected)entry.revision=next.settings.revision;
      return next;});
  };
  const switchAssignments = (enabled: boolean) => {
    const expected=status.settings.revision;
    void mutate(async()=>{const next=await saveAssignments(enabled,expected);
      if(baseRevision.current===expected)baseRevision.current=next.settings.revision;
      for(const entry of Object.values(drafts.current))if(entry.revision===expected)entry.revision=next.settings.revision;
      return next;});
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
  const save=()=>void mutate(()=>saveCurve(scope,clone(draft),baseRevision.current),true);
  if(fullscreen) return <div ref={pageRoot} data-fan-page="true" data-fan-dialog-body="true" style={{minHeight:0,flex:1,display:"flex",flexDirection:"column",gap:12}}>
    <DialogBody style={{minHeight:0,flex:1,overflowY:"auto",margin:0,padding:0,display:"flex",flexDirection:"column",gap:8}}>
      <div data-fan-dialog-status="true" title="APU temperature uses the higher of CPU Tctl and GPU edge.">{fanReadingLabel(status,Math.max(readingNow,Date.now()))} · {fanMode(status)}</div>
      <Focusable flow-children="row" data-fan-controls="true" style={{display:"grid",gridTemplateColumns:`repeat(${assignmentMode ? 4 : 3},minmax(0,1fr))`,gap:16,margin:0,flexShrink:0}}>
        {assignmentMode && <div><DialogLabel>Curve</DialogLabel><Dropdown menuLabel="Curve" selectedOption={scope} rgOptions={curveOptions} disabled={busy} onChange={option=>choose(option.data)} /></div>}
        <div><DialogLabel>Starting curve</DialogLabel><Dropdown menuLabel="Starting curve" selectedOption={matchingTemplate} rgOptions={templateOptions} disabled={busy} onChange={option=>applyTemplate(option.data)} /></div>
        <div><DialogLabel>Speed unit</DialogLabel><Dropdown menuLabel="Speed unit" selectedOption={unit} rgOptions={[{data:"rpm",label:"RPM"},{data:"percent",label:"Percent"}]} disabled={busy} onChange={option=>switchUnit(option.data==="percent")} /></div>
        <div><DialogLabel>Speed edit step</DialogLabel><Dropdown menuLabel="Speed edit step" selectedOption={displayedStep} rgOptions={stepOptions} disabled={busy} onChange={option=>setRpmStep(unit==="percent" ? Math.round(option.data*limits.max_rpm/100) : option.data)} /></div>
      </Focusable>
      {assignmentMode && <div data-fan-assignment-note="true" style={{color:"#66c0f4",fontSize:13}}>{ASSIGNMENT_NOTE}</div>}
      <div data-fan-dialog-graph="true" style={{minHeight:260,flex:"1 0 260px",maxHeight:480}}>{graph}</div>
      {notice && <div role="status">{notice}</div>}
      {scope!=="global" && status.settings.presets[scope] && <DialogButton disabled={busy || dirty} onClick={()=>void mutate(()=>saveCurve(scope,null,baseRevision.current),true)}>Use global curve for {title(scope)}</DialogButton>}
      <div style={{fontSize:13,color:"#afb7c0"}}>7 editable points · 100°C / full speed is fixed{dirty ? " · Unsaved changes" : ""}</div>
    </DialogBody>
    <DialogFooter style={{display:"flex",gap:12,margin:0,padding:0,flexShrink:0}}>
      <Focusable flow-children="row" data-fan-footer="true" style={{display:"flex",gap:12,width:"100%"}}>
      <DialogButton style={{flex:1}} disabled={saveDisabled} onClick={save}>{scope==="global" ? "Save global curve" : `Assign curve to ${title(scope)}`}</DialogButton>
      <DialogButton style={{flex:1}} disabled={busy || !dirty} onClick={discard}>Discard changes</DialogButton>
      <DialogButton style={{flex:1}} onClick={onClose}>Back to Fan Control</DialogButton>
      </Focusable>
    </DialogFooter>
  </div>;
  return <div ref={pageRoot} data-fan-page="true">
    <PanelSection>
      <div data-fan-summary="true" title="APU temperature uses the higher of CPU Tctl and GPU edge." style={{textAlign:"center",padding:"4px 0 12px"}}>
        <div style={{fontSize:20,fontWeight:600}}>{fanReadingLabel(status,Math.max(readingNow,Date.now()))}</div>
        <div style={{fontSize:13,color:"#afb7c0",marginTop:4}}>{fanMode(status)}</div>
      </div>
      <PanelSectionRow><ToggleField label="Enable Fan Control" checked={status.settings.enabled}
        disabled={busy || !status.supported || (!status.settings.enabled && !!status.ownership?.blocked) || ((dirty || status.needs_curve_review) && !status.settings.enabled)}
        description={status.ownership?.blocked && !status.settings.enabled ? status.ownership.reason : assignmentMode ? "Global curve with optional Companion TDP assignments." : "Uses your saved global curve."}
        onChange={enabled => void mutate(() => setEnabled(enabled))} /></PanelSectionRow>
      {(status.error || telemetry?.error || status.readings?.error) && <PanelSectionRow><Field focusable label="Fan Control needs attention"
        description={status.error || telemetry?.error || status.readings?.error} /></PanelSectionRow>}
      {notice && <PanelSectionRow><Field focusable label="Fan Control status" description={notice} /></PanelSectionRow>}
    </PanelSection>
    <PanelSection title={dirty ? "Curve · unsaved" : undefined}>
      {assignmentMode && <PanelSectionRow><DropdownItem label="Curve" selectedOption={scope} disabled={busy}
        rgOptions={["global",...status.presets].map(p=>({data:p,label:`${title(p)}${p!=="global" && !status.settings.presets[p] ? " · inherits global" : ""}${drafts.current[p]?.dirty ? " · unsaved" : ""}`}))}
        onChange={option=>choose(option.data)} /></PanelSectionRow>}
      <PanelSectionRow><DropdownItem label="Starting curve" selectedOption={matchingTemplate} disabled={busy}
        rgOptions={templateOptions} onChange={option=>applyTemplate(option.data)} /></PanelSectionRow>
      <PanelSectionRow>{graph}</PanelSectionRow>
      <PanelSectionRow><DropdownItem label="Speed edit step" selectedOption={displayedStep} disabled={busy}
        rgOptions={(unit === "percent" ? PERCENT_EDIT_STEPS : RPM_EDIT_STEPS).map(step=>({data:step,label:unit === "percent" ? `${step}%` : `${step} RPM`}))}
        onChange={option=>setRpmStep(unit === "percent" ? Math.round(option.data*limits.max_rpm/100) : option.data)} /></PanelSectionRow>
      {!fullscreen && <PanelSectionRow><ButtonItem layout="below" disabled={busy} onClick={openFull}>Full screen curve editor</ButtonItem></PanelSectionRow>}
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || (!dirty && !status.needs_curve_review && (scope === 'global' || !!status.settings.presets[scope]))}
        onClick={() => void mutate(() => saveCurve(scope, clone(draft), baseRevision.current), true)}>
        {scope === "global" ? "Save global curve" : `Assign curve to ${title(scope)}`}</ButtonItem></PanelSectionRow>
      <PanelSectionRow><ButtonItem layout="below" disabled={busy || !dirty} onClick={discard}>Discard changes</ButtonItem></PanelSectionRow>
      {scope !== "global" && status.settings.presets[scope] && <PanelSectionRow><ButtonItem layout="below" disabled={busy || dirty}
        onClick={() => void mutate(() => saveCurve(scope, null, baseRevision.current), true)}>Use global curve for {title(scope)}</ButtonItem></PanelSectionRow>}
    </PanelSection>
    <PanelSection title="Settings">
      <PanelSectionRow><ToggleField label="Show fan speed as percentage" checked={unit === "percent"} disabled={busy}
        description="RPM or percent in every fan view." onChange={switchUnit} /></PanelSectionRow>
      <PanelSectionRow><ToggleField label="Assign curves to TDP presets" checked={assignmentMode} disabled={busy || !!status.needs_curve_review}
        description="Keep using the global curve, or enable optional preset assignments." onChange={switchAssignments} /></PanelSectionRow>
      <PanelSectionRow><Field focusable label="TDP curve assignments" description={<span data-fan-assignment-note="true" style={{color:"#66c0f4"}}>{ASSIGNMENT_NOTE}</span>} /></PanelSectionRow>
    </PanelSection>
  </div>;
};
