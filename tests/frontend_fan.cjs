const complete = require('./frontend_test_guard.cjs')();
// Execute the actual FanPage with controlled React hooks/RPC timing.
const fs=require('node:fs'), path=require('node:path'), vm=require('node:vm'), assert=require('node:assert/strict');
const ts=require('../node_modules/typescript');
const curve=[[50,3000],[57,3120],[64,3540],[71,3960],[78,4440],[85,4800],[93,4800],[100,4800]];
const initial={supported:true,error:'',active_curve:'global',settings:{enabled:false,assign_tdp_presets:true,revision:0,display_unit:'rpm',global_curve:curve,presets:{}},
  templates:{silent:curve.map(([t,r],i)=>[t,i<7?0:r]),balanced:curve,aggressive:curve.map(([t])=>[t,4800])},
  presets:['silent','balanced','performance','max','custom'],limits:{min_rpm:1500,max_rpm:4800,step:1,hard_max:100},telemetry:{active:false,automatic:true},
  readings:{valid:true,rpm:1600,apu_temperature:44.5,age_seconds:0,automatic:true,error:''}};
const json=x=>JSON.parse(JSON.stringify(x)), same=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>v===b[i]);
const flush=async()=>{for(let i=0;i<30;i++)await Promise.resolve();};
function harness(initialState=initial){
  const slots=[],effects=new Map(),pending=[],timers=new Map(),calls=[];
  let cursor=0,visible=true,timerId=0,state=json(initialState), read=null, failSave=false,now=100000,modal=null;
  const hooks={useState(v){const i=cursor++;if(!(i in slots))slots[i]=typeof v==='function'?v():v;return[slots[i],n=>{slots[i]=typeof n==='function'?n(slots[i]):n;}];},
    useRef(v){const i=cursor++;return slots[i]??=( {current:v});},useEffect(fn,deps){const i=cursor++,prev=effects.get(i);if(!prev||!same(prev.deps,deps))pending.push(()=>{prev?.cleanup?.();effects.set(i,{deps,cleanup:fn()});});}};
  const mod={exports:{}};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(path.join(__dirname,'../src/fan.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,jsx:ts.JsxEmit.ReactJSX}}).outputText,{
    module:mod,exports:mod.exports,console,Date:class extends Date{static now(){return now;}},
    setTimeout(fn,ms){const id=++timerId;timers.set(id,{fn,ms,kind:'timeout'});return id;},clearTimeout:id=>timers.delete(id),
    setInterval(fn,ms){const id=++timerId;timers.set(id,{fn,ms,kind:'interval'});return id;},clearInterval:id=>timers.delete(id),
    require(name){if(name==='react')return hooks;if(name==='react/jsx-runtime')return{jsx:(type,props)=>({type,props}),jsxs:(type,props)=>({type,props})};
      if(name==='@decky/ui')return new Proxy({GamepadButton:{OK:1,CANCEL:2,BUMPER_LEFT:5,BUMPER_RIGHT:6,DIR_UP:9,DIR_DOWN:10,DIR_LEFT:11,DIR_RIGHT:12},showModal:(node,parent,props)=>{modal={node,props,Close(){props.fnOnClose?.();}};return modal;}}, {get:(o,k)=>k in o?o[k]:k});
      if(name==='@decky/api')return{useQuickAccessVisible:()=>visible,callable:name=>async(...args)=>{
        calls.push({name,args:json(args)});
        if(name==='fan_get_state')return read?read():json(state);
        if(name==='fan_set_enabled'){state.settings.enabled=args[0];state.settings.revision++;state.telemetry.active=args[0];state.telemetry.automatic=!args[0];}
        if(name==='fan_save_assignment_mode'){assert.equal(args[1],state.settings.revision);state.settings.assign_tdp_presets=args[0];state.settings.revision++;}
        if(name==='fan_save_unit'){assert.equal(args[1],state.settings.revision);state.settings.display_unit=args[0];state.settings.revision++;}
        if(name==='fan_save_curve'){
          if(failSave)throw Error('save rejected');assert.equal(args[2],state.settings.revision,'CAS revision');
          if(args[0]==='global')state.settings.global_curve=json(args[1]);else if(args[1])state.settings.presets[args[0]]=json(args[1]);else delete state.settings.presets[args[0]];
          state.settings.revision++;
          state.needs_curve_review=false;
        }return json(state);
      }};throw Error(name);}
  });
  return{exports:mod.exports,calls,timers,render(component=mod.exports.FanPage,props={}){cursor=0;const tree=component(props);pending.splice(0).forEach(f=>f());return tree;},
    hide(){visible=false;return this.render();},show(){visible=true;return this.render();},unmount(){for(const e of effects.values())e.cleanup?.();effects.clear();},
    poll(){const [id,t]=[...timers].find(([,t])=>t.kind==='timeout');timers.delete(id);t.fn();},
    advance(ms){now+=ms;for(const t of timers.values())if(t.kind==='interval')t.fn();},
    setRead(fn){read=fn;},failSave(v){failSave=v;},getState:()=>json(state),getModal:()=>modal};
}
function find(n,p){if(!n||typeof n!=='object')return;if(p(n))return n;for(const c of Array.isArray(n)?n:[n.props?.children,n.props?.description]){const r=find(c,p);if(r)return r;}}
const label=(tree,name)=>find(tree,n=>n.props?.label===name);
const button=(tree,name)=>find(tree,n=>n.type==='ButtonItem'&&(Array.isArray(n.props.children)?n.props.children.join(''):n.props.children)===name);
const text=node=>node==null||node===false?'':typeof node!=='object'?String(node):
  (Array.isArray(node)?node:[node.props?.children]).map(text).join(' ');
function centeredReadout(parent,child){
  const outer=parent.props.style??{},inner=child.props.style??{};
  if((inner.textAlign??outer.textAlign)==='center')return true;
  // An intrinsic-width, single-line child is also centered by a column flex
  // container. A stretched or explicitly left-aligned child is not equivalent.
  return outer.display==='flex' && outer.flexDirection==='column'
    && (inner.alignSelf??outer.alignItems)==='center'
    && (inner.width===undefined || inner.width==='auto')
    && (inner.textAlign===undefined || inner.textAlign==='center');
}
(async()=>{
  const h=harness();h.render();await flush();let tree=h.render();
  assert.equal(h.calls.filter(c=>c.name==='fan_get_state').length,1);assert.equal(h.timers.size,2);
  const summary=find(tree,n=>n.props?.['data-fan-summary']);
  const telemetry=find(summary,n=>n!==summary && n.props && text(n)==='APU 44.5°C · 1600 RPM');
  assert.ok(telemetry,'The summary shows measured temperature and tachometer values');
  assert.ok(centeredReadout(summary,telemetry),'Measured temperature and tachometer remain horizontally centered');
  assert.equal(label(tree,'Enable Fan Control').props.checked,false);
  assert.equal(find(tree,n=>n.type==='SliderField'),undefined,'No slider editor');
  assert.equal(button(tree,'Quick edit with controller'),undefined,'QAM enters editing only by activating the focused graph');
  assert.equal(label(tree,'Curve behavior'),undefined,'Detailed policy does not crowd the menu');
  assert.equal(label(tree,'Speed edit step').props.selectedOption,100,'Default manual edit step is 100 RPM');
  assert.ok(label(tree,'Curve').props.rgOptions.some(o=>o.data==='custom'),'Custom and Maximum remain independent curve assignments');
  const limits=initial.limits;
  const simple=harness({...initial,settings:{...initial.settings,assign_tdp_presets:false}});
  simple.render();await flush();let st=simple.render();
  assert.equal(label(st,'Curve'),undefined,'Default global mode hides TDP scope selection');
  assert.equal(label(st,'Assign curves to TDP presets').props.checked,false);
  assert.ok(find(st,n=>n.type==='PanelSection'&&n.props.title==='Settings'));
  assert.match(find(st,n=>n.props?.['data-fan-assignment-note']).props.children,/Companion TDP presets, not SteamOS QAM Performance/);
  const callsBefore=simple.calls.length,untouched=simple.getState();
  label(st,'Starting curve').props.onChange({data:'aggressive'});st=simple.render();
  assert.equal(simple.calls.length,callsBefore,'Selecting a template performs no RPC');
  assert.deepEqual(simple.getState(),untouched,'Selecting a template does not save or arm');
  assert.equal(label(st,'Starting curve').props.selectedOption,'aggressive','Template label is derived from exact draft points');
  find(st,n=>n.type===simple.exports.CurveGraph).props.edit(1,4799,0);st=simple.render();
  assert.equal(label(st,'Starting curve').props.selectedOption,'','An edited curve is never labelled as the former template');
  assert.equal(button(st,'Save global curve').props.disabled,false);
  const globalDraft=json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve);
  label(st,'Assign curves to TDP presets').props.onChange(true);await flush();st=simple.render();
  assert.ok(label(st,'Curve'));label(st,'Curve').props.onChange({data:'silent'});st=simple.render();
  label(st,'Starting curve').props.onChange({data:'silent'});st=simple.render();
  const silentDraft=json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve);
  label(st,'Assign curves to TDP presets').props.onChange(false);await flush();st=simple.render();
  assert.equal(label(st,'Curve'),undefined);
  assert.deepEqual(json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve),globalDraft,'Disabling assignments restores the unsaved global draft');
  label(st,'Assign curves to TDP presets').props.onChange(true);await flush();st=simple.render();
  label(st,'Curve').props.onChange({data:'silent'});st=simple.render();
  assert.deepEqual(json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve),silentDraft,'Re-enabling assignments retains unsaved TDP drafts');
  button(st,'Assign curve to Silent').props.onClick();await flush();st=simple.render();
  assert.deepEqual(simple.getState().settings.presets.silent,silentDraft);
  assert.deepEqual(simple.getState().templates,untouched.templates,'Drafts never alias hardcoded template arrays');
  assert.equal(simple.getState().settings.enabled,false);
  assert.equal(simple.getState().settings.template,undefined);
  label(st,'Curve').props.onChange({data:'global'});st=simple.render();
  button(st,'Full screen curve editor').props.onClick();st=simple.render();
  const fm=simple.getModal(),editor=harness(simple.getState());
  const fullPropsTemplates={fullscreen:true,initialEditor:fm.node.props.initial,onEditorChange:fm.node.props.onResult};
  editor.render(undefined,fullPropsTemplates);await flush();let et=editor.render(undefined,fullPropsTemplates);
  find(et,n=>n.type==='Dropdown'&&n.props.menuLabel==='Starting curve').props.onChange({data:'balanced'});et=editor.render(undefined,fullPropsTemplates);
  find(et,n=>n.type===editor.exports.CurveGraph).props.edit(1,2999,0);et=editor.render(undefined,fullPropsTemplates);
  const editedFull=json(find(et,n=>n.type===editor.exports.CurveGraph).props.curve);
  find(et,n=>n.type==='DialogButton'&&n.props.children==='Save global curve').props.onClick();await flush();et=editor.render(undefined,fullPropsTemplates);
  assert.deepEqual(editor.getState().settings.global_curve,editedFull,'Fullscreen template edits save through ordinary curve RPC');
  simple.setRead(async()=>editor.getState());fm.Close();simple.render();await flush();st=simple.render();
  assert.deepEqual(json(find(st,n=>n.type===simple.exports.CurveGraph).props.curve),editedFull,'Saved fullscreen draft synchronizes with parent');
  assert.equal(button(st,'Save global curve').props.disabled,true);
  editor.unmount();simple.unmount();
  for(const [width,height] of [[1040,266],[900,220],[1600,480]]){
    const m=h.exports.graphLayout(true,width,height),scale=Math.min(width/m.width,height/m.height);
    assert.ok(Object.values(m).every(value=>Number.isFinite(value) && value>0),'Fullscreen transforms remain finite');
    assert.ok(m.left+m.span<=m.width && m.rise<=m.bottom && m.bottom<=m.height,'Plot coordinates stay inside the graph');
    assert.ok(m.span*scale<=width && m.rise*scale<=height,'Scaled plot fits its viewport');
    // Readability and Steam overlay clearance require rendered visual review;
    // model coordinates do not establish a font size or a screen layout.
  }
  assert.equal(h.exports.editPoint(curve,0,0,51,limits)[0][0],51);
  assert.equal(h.exports.editPoint(curve,1,0,52,limits)[1][0],52);
  assert.equal(h.exports.editPoint(curve,7,0,99,limits)[7][0],100);
  assert.equal(h.exports.editPoint(curve,7,1,0,limits)[7][1],4800);
  assert.equal(h.exports.editPoint(curve,4,1,1501,limits)[4][1],3960,'Lower neighbour bounds RPM');
  assert.equal(h.exports.editPoint(curve,0,1,100,limits)[0][1],0,'Deadband descending snaps to stop');
  const stopped=json(curve);stopped[0][1]=0;
  assert.equal(h.exports.editPoint(stopped,0,1,1,limits)[0][1],1500,'Deadband rising snaps to running minimum');

  assert.equal(h.exports.editPoint(curve,0,0,-50,limits)[0][0],0);
  assert.equal(h.exports.editPoint(curve,0,0,100,limits)[0][0],56);
  assert.equal(h.exports.editPoint(curve,4,1,9999,limits)[4][1],4800);
  assert.equal(h.exports.editPoint(curve,2,1,9999,limits)[2][1],3960);
  const zeroPair=json(curve);zeroPair[0][1]=zeroPair[1][1]=0;
  assert.equal(h.exports.editPoint(zeroPair,0,1,1,limits)[0][1],0,'A following stopped point prevents a rising deadband snap');
  assert.equal(h.exports.editPoint(curve,2,1,0,limits)[2][1],3120,'Cannot stop after a running neighbour');
  for(const delta of [-5000,-500,-100,-48,-1,1,48,100,500,5000])for(let i=0;i<7;i++){
    const result=h.exports.editPoint(curve,i,1,curve[i][1]+delta,limits);
    assert.ok(result.every((p,j)=>j===0||p[1]>=result[j-1][1]));
    assert.ok(result.every(p=>p[1]===0||p[1]>=1500));
  }
  const steps=harness();steps.render();await flush();let stepTree=steps.render();
  const assertStep=()=>{
    const dropdown=label(stepTree,'Speed edit step');
    assert.ok(dropdown.props.rgOptions.some(o=>o.data===dropdown.props.selectedOption),'Every displayed step has a real dropdown option');
    const factor=steps.getState().settings.display_unit==='percent' ? 48 : 1;
    assert.equal(find(stepTree,n=>n.type===steps.exports.CurveGraph).props.rpmStep,dropdown.props.selectedOption*factor,'Graph edits use the displayed step');
  };
  for(const rpm of [1,10,50,100,200,500]){
    label(stepTree,'Speed edit step').props.onChange({data:rpm});stepTree=steps.render();assertStep();
    label(stepTree,'Show fan speed as percentage').props.onChange(true);await flush();stepTree=steps.render();assertStep();
    label(stepTree,'Show fan speed as percentage').props.onChange(false);await flush();stepTree=steps.render();assertStep();
  }
  label(stepTree,'Show fan speed as percentage').props.onChange(true);await flush();stepTree=steps.render();
  for(const percent of [1,2,5,10]){
    label(stepTree,'Speed edit step').props.onChange({data:percent});stepTree=steps.render();assertStep();
    label(stepTree,'Show fan speed as percentage').props.onChange(false);await flush();stepTree=steps.render();assertStep();
    label(stepTree,'Show fan speed as percentage').props.onChange(true);await flush();stepTree=steps.render();assertStep();
  }
  assert.deepEqual(steps.getState().settings.global_curve,curve,'Changing edit steps and units never changes the curve');steps.unmount();

  let graph=find(tree,n=>n.type===h.exports.CurveGraph);graph.props.edit(0,51,0);tree=h.render();
  assert.equal(button(tree,'Save global curve').props.disabled,false);
  label(tree,'Curve').props.onChange({data:'silent'});tree=h.render();
  graph=find(tree,n=>n.type===h.exports.CurveGraph);graph.props.edit(1,1501,2);tree=h.render();
  label(tree,'Curve').props.onChange({data:'global'});tree=h.render();
  assert.equal(find(tree,n=>n.type===h.exports.CurveGraph).props.curve[0][0],51,'Changing scope retains unsaved drafts');
  label(tree,'Show fan speed as percentage').props.onChange(true);await flush();tree=h.render();
  assert.match(find(tree,n=>n.props?.['data-fan-summary']).props.children[0].props.children,/33.3%/);
  assert.equal(h.exports.speedLabel(4800,h.getState()),'100.0%');
  assert.equal(h.exports.speedLabel(0,h.getState()),'0.0%');
  assert.equal(h.exports.speedLabel(undefined,h.getState()),'—%');
  assert.equal(find(tree,n=>n.type===h.exports.CurveGraph).props.curve[0][0],51,'Units preserve dirty curve');
  let finishRead;const old=h.getState();h.setRead(()=>new Promise(r=>finishRead=r));h.poll();await flush();
  button(tree,'Save global curve').props.onClick();await flush();tree=h.render();
  assert.equal(h.getState().settings.global_curve[0][0],51);
  finishRead(old);await flush();tree=h.render();
  assert.equal(find(tree,n=>n.type===h.exports.CurveGraph).props.curve[0][0],51,'Old read cannot revert confirmed save');
  h.setRead(null);
  label(tree,'Curve').props.onChange({data:'silent'});tree=h.render();
  h.failSave(true);button(tree,'Assign curve to Silent').props.onClick();await flush();tree=h.render();
  assert.match(label(tree,'Fan Control status').props.description,/save rejected/);
  assert.equal(find(tree,n=>n.type===h.exports.CurveGraph).props.curve[2][1],3120,'Failed save retains draft');
  h.failSave(false);button(tree,'Assign curve to Silent').props.onClick();await flush();tree=h.render();
  assert.equal(h.getState().settings.presets.silent[2][1],3120);
  button(tree,'Use global curve for Silent').props.onClick();await flush();assert.equal(h.getState().settings.presets.silent,undefined);
  tree=h.render();button(tree,'Full screen curve editor').props.onClick();tree=h.render();
  assert.equal(h.timers.size,0,'Parent does no polling behind full-screen modal');
  assert.ok(h.getModal());h.getModal().Close();h.render();await flush();h.render();assert.equal(h.timers.size,2);
  h.hide();assert.equal(h.timers.size,0);h.unmount();

  const g=harness();let selected=0,edited=[];
  const props={curve,status:{...initial,receivedAt:100000},selected,disabled:false,rpmStep:1,select:i=>selected=i,edit:(...args)=>edited.push(args)};
  let graphics=g.render(g.exports.CurveGraph,props),svg=find(graphics,n=>n.type==='svg');
  assert.equal(graphics.props.tabIndex,0,'The graph must be an eligible native keyboard/gamepad focus target');
  assert.equal(find(svg,n=>n.props?.['data-fan-point']===8).type,'circle');
  assert.ok(find(svg,n=>n.props?.['data-fan-live-marker']),'Fresh actual tach marker shown');
  const event=button=>({detail:{button},preventDefault(){this.prevented=true;},stopPropagation(){this.stopped=true;}});
  assert.equal(graphics.props.onGamepadDirection,undefined,'Normal navigation remains available outside edit mode');
  graphics.props.onOKButton(event(1));graphics=g.render(g.exports.CurveGraph,props);
  graphics.props.onActivate(event(1));graphics=g.render(g.exports.CurveGraph,props);
  assert.equal(graphics.props['data-fan-editing'],'true','Activate and OK both enter idempotently');
  graphics.props.onButtonDown(event(6));assert.equal(selected,1);
  props.selected=1;graphics=g.render(g.exports.CurveGraph,props);
  graphics.props.onButtonDown(event(5));assert.equal(selected,0,'LB selects the previous point');
  props.selected=0;graphics=g.render(g.exports.CurveGraph,props);
  graphics.props.onButtonDown(event(5));assert.equal(selected,7,'LB wraps from the first point to the fixed last point');
  props.selected=7;graphics=g.render(g.exports.CurveGraph,props);
  graphics.props.onButtonDown(event(6));assert.equal(selected,0,'RB wraps from the last point to the first');
  props.selected=1;graphics=g.render(g.exports.CurveGraph,props);
  graphics.props.onButtonDown(event(12));assert.equal(edited.length,0,'ButtonDown must not duplicate the native direction path');
  graphics.props.onGamepadDirection(event(12));graphics.props.onGamepadDirection(event(9));
  assert.deepEqual(json(edited),[[0,58],[1,3121]],'D-pad edits temperature by one degree and selected speed step');
  const pointerSvg=find(graphics,n=>n.type==='svg');
  pointerSvg.props.ref.current={getScreenCTM:()=>({inverse:()=>({})}),createSVGPoint:()=>({matrixTransform(){return{x:this.x,y:this.y};}}),setPointerCapture(){}};
  const hit=find(pointerSvg,n=>n.type==='g'&&n.props.onPointerDown);
  hit.props.onPointerDown({pointerId:1,preventDefault(){}});
  // Drag to the independently specified 52°C / 3000 RPM using the rendered
  // first/last point locations. This survives plot-margin/layout changes.
  const firstPoint=find(pointerSvg,n=>n.props?.['data-fan-point']===1).props;
  const lastPoint=find(pointerSvg,n=>n.props?.['data-fan-point']===8).props;
  const clientX=firstPoint.cx+(52-curve[0][0])/(curve[7][0]-curve[0][0])*(lastPoint.cx-firstPoint.cx);
  edited=[];pointerSvg.props.onPointerMove({pointerId:1,clientX,clientY:firstPoint.cy});
  assert.deepEqual(json(edited).map(([axis,,point])=>[axis,point]),[[0,0],[1,0]],'Pointer drag keeps the chosen point and updates both coordinates');
  assert.ok(Math.abs(edited[0][1]-52)<1e-9,'Pointer temperature follows the rendered position');
  assert.equal(edited[1][1],3000,'Pointer speed follows the rendered position');
  pointerSvg.props.onLostPointerCapture();edited=[];
  pointerSvg.props.onPointerMove({pointerId:1,clientX:100,clientY:100});
  assert.equal(edited.length,0,'Losing capture must end a drag before mouse hover can edit again');
  const b=event(2);graphics.props.onCancelButton(b);assert.ok(b.prevented && b.stopped,'B exits steering without leaving module');
  graphics=g.render(g.exports.CurveGraph,props);assert.equal(graphics.props.onCancelButton,undefined,'Next B follows native navigation');
  assert.equal(graphics.props.onGamepadFocus,undefined,'Focus alone does not enter edit mode');
  assert.equal(graphics.props['data-fan-editing'],'false');
  graphics.props.onOKButton(event(1));graphics=g.render(g.exports.CurveGraph,props);
  assert.equal(graphics.props['data-fan-editing'],'true','A explicitly enters controller editing');
  graphics.props.onGamepadBlur(event(0));graphics=g.render(g.exports.CurveGraph,props);assert.equal(graphics.props.onGamepadDirection,undefined,'Blur exits edit mode');
  props.status={...props.status,receivedAt:96000};graphics=g.render(g.exports.CurveGraph,props);
  assert.equal(find(graphics,n=>n.props?.['data-fan-live-marker']),undefined,'Stale sample removes marker');g.unmount();

  const wide=harness();let wideEdits=[];
  const wideProps={...props,status:{...initial,receivedAt:100000},fullscreen:true,selected:1,edit:(...args)=>wideEdits.push(args)};
  const wideTree=wide.render(wide.exports.CurveGraph,wideProps),wideSvg=find(wideTree,n=>n.type==='svg');
  const metrics=wide.exports.graphLayout(true);
  wideSvg.props.ref.current={getScreenCTM:()=>({inverse:()=>({})}),createSVGPoint:()=>({matrixTransform(){return{x:(this.x-40)/1.5,y:(this.y-60)/1.5};}}),setPointerCapture(){}};
  const wideHit=find(wideSvg,n=>n.type==='g'&&n.props.children[0].props['data-fan-point']===2);
  wideHit.props.onPointerDown({pointerId:7,preventDefault(){}});
  wideSvg.props.onPointerMove({pointerId:7,clientX:40+1.5*(metrics.left+56*metrics.span/100),clientY:60+1.5*(metrics.bottom-2999/4800*metrics.rise)});
  assert.ok(Math.abs(wideEdits[0][1]-56)<1e-9,'Fullscreen inverse transform uses the whole 0–100°C span');
  assert.deepEqual(json(wideEdits).map(([axis,value,point])=>[axis,Math.round(value),point]),[[0,56,1],[1,2999,1]],'Fullscreen pointer maps scaled screen coordinates to the chosen one-RPM edit before neighbour clamping');
  wideSvg.props.onPointerCancel();wideEdits=[];
  wideSvg.props.onPointerMove({pointerId:7,clientX:100,clientY:100});assert.equal(wideEdits.length,0,'Cancelled fullscreen drag cannot edit');wide.unmount();
  const full=harness();let closed=0;const fullProps={fullscreen:true,onClose:()=>closed++};
  let fullTree=full.render(undefined,fullProps);
  await flush();fullTree=full.render(undefined,fullProps);
  assert.ok(find(fullTree,n=>n.type==='DialogBody'));assert.ok(find(fullTree,n=>n.type==='DialogFooter'));
  for(const attr of ['data-fan-controls','data-fan-footer'])assert.equal(find(fullTree,n=>n.props?.[attr]).props['flow-children'],'row','Horizontal controls use native horizontal focus flow');
  assert.equal(find(fullTree,n=>n.type==='DialogButton'&&n.props.children==='Quick edit with controller'),undefined);
  assert.equal(find(fullTree,n=>n.type===full.exports.CurveGraph).props.fullscreen,true,'The fullscreen view keeps the same editable graph');
  const units=find(fullTree,n=>n.type==='Dropdown'&&n.props.menuLabel==='Speed unit');units.props.onChange({data:'percent'});await flush();fullTree=full.render(undefined,fullProps);
  assert.equal(full.getState().settings.display_unit,'percent','Dedicated dialog retains persistent units');
  find(fullTree,n=>n.type==='DialogButton'&&n.props.children==='Back to Fan Control').props.onClick();assert.equal(closed,1);full.unmount();

  const blocked=harness({...initial,ownership:{blocked:true,state:'unavailable',reason:'Another process owns raw EC ports.'}});
  blocked.render();await flush();tree=blocked.render();assert.equal(label(tree,'Enable Fan Control').props.disabled,true);
  assert.match(label(tree,'Enable Fan Control').props.description,/Another process/);blocked.unmount();
  const recovered=harness();recovered.render();await flush();recovered.render();
  recovered.setRead(()=>Promise.reject(Error('transient read reset')));recovered.poll();await flush();
  tree=recovered.render();assert.match(label(tree,'Fan Control status').props.description,/transient read reset/);
  recovered.setRead(null);recovered.poll();await flush();tree=recovered.render();
  assert.equal(label(tree,'Fan Control status'),undefined,'A successful read clears a resolved transient read error');
  assert.match(find(tree,n=>n.props?.['data-fan-summary']).props.children[0].props.children,/APU 44.5/);
  label(tree,'Curve').props.onChange({data:'silent'});tree=recovered.render();
  recovered.failSave(true);button(tree,'Assign curve to Silent').props.onClick();await flush();tree=recovered.render();
  assert.match(label(tree,'Fan Control status').props.description,/save rejected/);
  recovered.poll();await flush();tree=recovered.render();
  assert.match(label(tree,'Fan Control status').props.description,/save rejected/,'Background read recovery does not erase a rejected user mutation');
  recovered.unmount();
  const expire=harness();expire.render();await flush();tree=expire.render();
  let pendingResolve;expire.setRead(()=>new Promise(r=>pendingResolve=r));expire.poll();await flush();
  expire.advance(3000);tree=expire.render();assert.match(find(tree,n=>n.props?.['data-fan-summary']).props.children[0].props.children,/APU —/);
  expire.hide();const count=expire.calls.length;pendingResolve(initial);await flush();assert.equal(expire.calls.length,count);expire.unmount();
  const late=harness();late.render();await flush();let end;late.setRead(()=>new Promise(r=>end=r));const pending=late.exports.getFanState();await flush();
  late.advance(3000);end(json(initial));assert.match(late.exports.fanSummary(await pending),/APU —/);late.unmount();
  const queued=harness();queued.render();await flush();const callbacks=[...queued.timers.values()].map(t=>t.fn),countQ=queued.calls.length;
  queued.hide();callbacks.forEach(fn=>fn());await flush();assert.equal(queued.calls.length,countQ);
  await assert.rejects(queued.exports.getFanState(()=>false),/no longer visible/);queued.unmount();
  console.log('Fan v3: eight points, ordered 0–100°C temperatures/monotonic RPM, zero/deadband, units, dirty scopes, CAS/failures/late reads, graph buttons, full-screen parent pause, conflict block, expiry and hidden cleanup passed.');
})().then(complete).catch(e=>{console.error(e);process.exitCode=1;});
