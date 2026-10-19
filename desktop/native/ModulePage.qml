// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "tdp.mjs" as Tdp
import "wifi.mjs" as Wifi
RowLayout {
    id:panel
    UiTheme { id:theme }
    required property var backend
    signal navigate(string page)
    property string page:"overview"
    property var cards:[]
    property string tdpScope:"global"
    property bool acSeparate:false
    // The Custom draft as in Decky: watts, boosts as headroom above SPL (see tdp.mjs).
    property var powerDraft:({spl:15,spptOff:5,fpptOff:10})
    property bool powerDirty:false
    readonly property bool editing:powerDirty
    property string powerNotice:""
    property string draftGame:""
    // A saved profile chosen in Game profiles: the TDP and CPU editors change it instead of the running game.
    property string editGame:""
    property string powerPreset:"custom"
    property string pendingMethod:""
    property var pendingArgs:[]
    property string confirmation:""
    property var pendingAction:null
    property bool customPicked:false
    property string applyingModule:""
    property real tick:backend.now()
    property bool controllerDetails:false
    property bool displaySwitch:false
    property bool displayDetails:false
    property var wifiCheck:null
    onTdpScopeChanged: { powerNotice=""; backend.cpuApp=powerTarget(); backend.cpuAc=tdpScope==="ac"; }
    // Several cards share two equal columns on a wide page: in Decky order each card goes to the
    // column that is shorter with typical content (fixed per card so nothing jumps while editing);
    // a lone card keeps a readable form width.
    readonly property int columns:cards.length>1 && width>=theme.twoColumns?2:1
    spacing:theme.gap
    function s(method) { return backend ? backend.state(method) : {}; }
    readonly property var firmwareBusy:({checking:"Checking…",downloading:"Downloading…",installing:"Installing…",verifying:"Verifying…"})
    function firmwareFresh() {
        const value=s("firmware_get_status");
        return tick>=0 && backend.connected && backend.age("firmware_get_status")<8 && !backend.readErrors?.firmware_get_status &&
            [value?.message,value?.error,value?.token,value?.install_reason].every(v=>v===undefined || typeof v==="string") &&
            ["idle","checking","up_to_date","available","downloading","downloaded","installing","verifying","installed","failed"].includes(value?.phase);
    }
    function firmwareMessage() { return firmwareFresh() ? s("firmware_get_status").error || s("firmware_get_status").message || "" : backend.readErrors?.firmware_get_status || "Wait for a fresh firmware status before updating."; }
    function firmwarePhase() { return firmwareFresh() ? {idle:"Controller firmware",checking:"Checking for updates",up_to_date:"Up to date",available:"Update available",downloading:"Downloading update",downloaded:"Update downloaded",installing:"Installing update",verifying:"Verifying update",installed:"Update installed",failed:"Update failed"}[s("firmware_get_status").phase] : "Status unavailable"; }
    // RPC lists arrive as Qt sequences, which Array.isArray() does not accept.
    function items(value) { return !!value && typeof value==="object" && typeof value.length==="number"?Array.from(value):[]; }
    function firmwareComponent(id) { return items((s("firmware_get_status") || {}).components).find(c=>c && c.id===id) || {}; }
    function firmwareRow(id) { const c=firmwareComponent(id);return "Installed "+(c.installed || "—")+" · Available "+(c.available || "—")+(c.newer?" · newer":""); }
    function firmwareRowVisible(id) { const c=firmwareComponent(id);return firmwareFresh() && !!(c.installed || c.available); }
    function firmwareInstallReason() { const value=s("firmware_get_status");return firmwareFresh() && value.phase==="downloaded" && value.install_enabled!==true ? value.install_reason || "" : ""; }
    function firmwareCanInstall(value) { return value.phase==="downloaded" && value.install_enabled===true; }
    function firmwareActionLabel() { const value=firmwareFresh()?s("firmware_get_status"):{};return firmwareBusy[value.phase] || (value.phase==="available"?"Download":firmwareCanInstall(value)?"Install":"Check for updates"); }
    function firmwareActionAvailable() {
        const value=s("firmware_get_status");
        return firmwareFresh() && (["idle","up_to_date","installed","failed","available"].includes(value.phase) ||
            (value.phase==="downloaded" && (value.install_enabled!==true || !!value.token)));
    }
    function firmwareAct() {
        if(!firmwareActionAvailable())return;
        const value=s("firmware_get_status");
        if(value.phase==="available")change("firmware_download");
        else if(firmwareCanInstall(value))confirm("firmware_install",[value.token],(value.components || []).filter(c=>c && c.newer).map(c=>c.label+": "+c.installed+" → "+c.available).join("\n")+
            "\n\nThe update takes about 5 minutes. Keep AC power connected and do not detach the controllers. Controller input may be unavailable until it finishes.");
        else change("firmware_check");
    }
    function change(method,args,then) { backend.mutate(method,args || [],then); }
    // Same titles, texts and OK labels as the Decky confirmations (UX spec 1.4).
    function confirm(method,args,text,title,okText,danger) {
        pendingMethod=method; pendingArgs=args; confirmation=text; pendingAction=null;
        dialog.title=title || "Confirm change"; dialog.okText=okText || "OK"; dialog.danger=!!danger; dialog.open();
    }
    // A confirmed local step (discarding a draft), not an RPC.
    function confirmAction(text,title,okText,action) { confirm("",[],text,title,okText,false); pendingAction=action; }
    function confirmed() {
        const action=pendingAction;pendingAction=null;
        if(action)action();else change(pendingMethod,pendingArgs);
    }
    function pretty(value) { const labels={fps:"FPS",spg:"SPG",rpg:"RPG",ac:"Plugged in",game:"This game"};const text=String(value).replace(/_/g," ");return labels[value] || text.charAt(0).toUpperCase()+text.slice(1); }
    function presetTable() { return s("get_caps").presets || Tdp.PRESETS; }
    function powerOptions() { const table=presetTable();return Tdp.PRESET_ORDER.filter(k=>k==="custom" || !!table[k]).map(k=>({id:k,label:Tdp.PRESET_LABELS[k]})); }
    function options(values) { return (values || []).map(v=>typeof v==="object"?v:{id:v,label:pretty(v)}); }
    function indexedOptions(values) { return (values || []).map((label,id)=>({id:id,label:pretty(label)})); }
    function field(label,kind,get,set,extra) { return Object.assign({label:label,kind:kind,get:get,set:set,available:()=>true},extra || {}); }
    function text(label,get,extra) { return field(label,"text",get,()=>{},extra); }
    function notice(label,get) { return field(label,label?"text":"paragraph",get,()=>{},{visible:()=>String(get() || "").length>0}); }
    function action(label,method,args,extra) { return field(label,"button",()=>label,()=>change(method,args?args():[]),extra); }
    function switcher(label,get,method,args,extra) { return field(label,"toggle",get,v=>change(method,args?args(v):[v]),extra); }
    function choices(label,get,set,values,extra) { return field(label,"choice",get,set,Object.assign({choices:values},extra || {})); }
    function number(label,get,set,min,max,extra) { return field(label,"range",get,set,Object.assign({minimum:min,maximum:max},extra || {})); }
    function percent(value) { return value===undefined || value===null || !Number.isFinite(Number(value))?"—":Math.round(Number(value))+"%"; }
    // Saved game profiles, by name: what each game uses and its limits (mW in the settings).
    function profileRows() {
        const rows=s("list_game_profiles").profiles,limit=(preset,spl)=>(preset?(Tdp.PRESET_LABELS[preset] || pretty(preset))+" · ":"")+watts(Number(spl)/1000)+" W";
        return items(rows).map(p=>{
            const id=String(p.app_id);
            return {id:id,title:gameTitle(id)+(id===running()?" · running":""),
                detail:p.active===false?"Global · its settings are kept: "+limit(p.preset,p.spl):
                    p.ac_separate?"This game + charger · Battery: "+limit(p.preset,p.spl)+" · Charger: "+limit(p.ac_preset,p.ac_spl):"This game · "+limit(p.preset,p.spl),
                actions:[{id:"edit",label:id===app()?"Editing":"Edit",enabled:id!==app()},{id:"delete",label:"Delete",tone:"danger"}]};
        }).sort((a,b)=>a.title.localeCompare(b.title));
    }
    function gameTitle(id) { return (backend.host && backend.host.gameName(id)) || (Number(id)>=2147483648?"Non-Steam game ":"App ")+id; }
    function profileAction(value) {
        if(value.action==="edit")editProfile(value.id);
        else confirm("delete_game_profile",[value.id],"Its power, CPU Boost and energy settings are removed. The game will use Global settings.","Delete profile for "+gameTitle(value.id)+"?","Delete",true);
    }
    function editProfile(id) {
        guardDraft(()=>{
            editGame=id===running()?"":id; backend.editApp=editGame; backend.gameProfileApp="";
            customPicked=false; tdpScope="global"; syncScope();
            if(app())backend.issue("get_game_profile",[app()]);
        });
    }
    // A saved profile is edited without touching the hardware, so never as the running game.
    function stored() { return !!editGame && editGame!==running(); }
    function storedEdit() { return stored() && tdpScope!=="global"; }
    function expectedGame() { return stored()?running():app(); }
    function watts(value) { return value===undefined || value===null || !Number.isFinite(Number(value))?"—":Number(value).toFixed(0); }
    function running() { return String(s("desktop_context").app_id || ""); }
    function app() { return editGame || running(); }
    function powerTarget() { return tdpScope==="global"?"":app(); }
    function profileReady() { return tdpScope==="global" || (!!app() && backend.gameProfileApp===app() && backend.age("get_game_profile")<8); }
    function gameProfile() { return !!app() && backend.gameProfileApp===app() ? s("get_game_profile") : {}; }
    function gameProfileFresh() { return !!app() && backend.gameProfileApp===app() && backend.age("get_game_profile")<8; }
    function acOnline() { return s("get_power_source").ac===true; }
    function loadPower() {
        if(!backend || powerDirty || !profileReady()) return;
        let source=s("get_settings");
        if(tdpScope!=="global" && s("get_game_profile").exists)
            source=tdpScope==="ac"?s("get_game_profile").ac_profile || source:s("get_game_profile").profile || source;
        // "Custom" picked here shows the sliders until something is applied; polling must not hide them.
        const w=Math.round(Number(source.spl || 15000)/1000),sw=Math.round(Number(source.sppt || 20000)/1000),fw=Math.round(Number(source.fppt || 25000)/1000);
        // As in Decky: the saved name while its values still match, otherwise the preset these values are.
        powerPreset=customPicked?"custom":Tdp.resolvedPreset(w,sw,fw,source.preset || source.ac_preset || source.active_preset,presetTable());
        // Without Separate boost limits a Custom profile follows the single-dial rule, as in Decky.
        powerDraft=!tdpAdvanced() && powerPreset==="custom"?Tdp.simpleTuning(w,customCaps(),tdpMin()):
            Tdp.fromAbsolute(w,sw,fw);
        if(s("get_game_profile").exists)acSeparate=!!s("get_game_profile").ac_separate;
        draftGame=app();
    }
    // What the running game uses: its saved profile decides; without one (or without a TDP provider to save
    // it), the local choice stands.
    function gameUse() {
        const profile=gameProfile();
        if(profile.exists)return profile.active===false?"global":profile.ac_separate?"charger":"game";
        return tdpScope==="global"?"global":acSeparate?"charger":"game";
    }
    function syncScope() {
        if(powerDirty)return;
        if(!app()) { tdpScope="global"; return; }
        if(!gameProfile().exists)return;
        const use=gameUse();
        if(use!=="charger")tdpScope=use;
        else if(tdpScope==="global")tdpScope=acOnline()?"ac":"game";
    }
    function scopeChanged() { customPicked=false; loadPower(); backend.issue("get_cpu_power_controls",[powerTarget(),tdpScope==="ac"]); }
    function guardDraft(action) {
        if(!powerDirty) { action(); return; }
        // The running game can change while the dialog is open; its choice must not land on the next game.
        const game=app();
        confirmAction("Your changes to these limits haven't been applied.","Discard unsaved changes?","Discard",()=>{
            powerDirty=false;
            // Without a game there is no game profile to wait for, only Global.
            if(app()===game && (!game || gameProfileFresh()))action();
            else backend.error="The running game changed. Choose again for this game.";
        });
    }
    function acArgs(separate) {
        const profile=gameProfile(),ac=profile.ac_profile || {},base=profile.profile || {};
        return [app(),Math.round(Number(ac.spl ?? base.spl)),Math.round(Number(ac.sppt ?? base.sppt)),Math.round(Number(ac.fppt ?? base.fppt)),separate,"",stored()];
    }
    function chooseUse(value) {
        if(value===gameUse())return;
        guardDraft(()=>{
            const profile=gameProfile();
            if(profile.exists) {
                // Turning the profile off keeps it, inactive; deleting it is a separate, confirmed action.
                if(value==="global")change("set_game_profile_active",[app(),false]);
                else if(profile.active===false) {
                    // The charger split follows the reactivation; its arguments are fixed now, for this game.
                    const separate=value==="charger"?true:profile.ac_separate?false:null;
                    change("set_game_profile_active",[app(),true],separate===null?null:{method:"set_game_ac_profile",args:acArgs(separate)});
                }
                else change("set_game_ac_profile",acArgs(value==="charger"));
            } else {
                acSeparate=value==="charger";
                // Like Decky, create the profile at once, and its charger split after it: CPU settings on charger need it.
                // The backend starts the profile from the Global settings, the same values this panel shows.
                const g=s("get_settings");
                if(value!=="global" && s("get_caps").tdp_available)change("set_game_profile_active",[app(),true],
                    acSeparate?{method:"set_game_ac_profile",args:[app(),g.spl,g.sppt,g.fppt,true,"",stored()]}:null);
            }
            tdpScope=value==="global"?"global":value==="charger" && acOnline()?"ac":"game";
            scopeChanged();
        });
    }
    function chooseEditTarget(value) {
        const next=value==="charger"?"ac":"game";
        if(next!==tdpScope)guardDraft(()=>{ tdpScope=next; scopeChanged(); });
    }
    function editTargetNote() { return Tdp.sourceNote(acOnline(),tdpScope==="ac"); }
    function choosePreset(value) {
        if(value==="custom") { customPicked=true; powerPreset="custom"; if(!tdpAdvanced())powerDraft=Tdp.simpleTuning(powerDraft.spl,customCaps(),tdpMin()); return; }
        const preset=presetTable()[value];
        if(!preset)return;
        // A preset replaces a Custom draft without asking: the sliders exist only in Custom.
        powerDirty=false; customPicked=false;
        powerDraft=Tdp.fromAbsolute(Number(preset.spl),Number(preset.sppt),Number(preset.fppt)); powerPreset=value;
        applyPower();
    }
    function applyLabel() { return tdpScope==="global"?"Apply":tdpScope==="ac"?"Apply for this game on charger":"Apply for this game"; }
    function extrasActive() { return s("get_extras_unlocked")===true && s("get_caps").extras!==false; }
    function tdpCaps() { const caps=s("get_caps"),value=extrasActive()?caps.max:caps.std;
        return {spl:Number(value?.spl || 5),sppt:Number(value?.sppt || 5),fppt:Number(value?.fppt || 5)}; }
    function powerLimit(key) { return tdpCaps()[key]; }
    function tdpMin() { return Number(s("get_caps").min || 5); }
    function tdpAdvanced() { return s("get_settings").advanced_tdp_control!==false; }
    function customCaps() { return tdpAdvanced()?tdpCaps():Tdp.simpleCaps(tdpCaps(),extrasActive()); }
    function boostRoom() { return Tdp.offsetMax(powerDraft.spl,tdpCaps()); }
    function boostNote(offset,room) { return Tdp.boostRoomNote(powerDraft.spl,offset,room); }
    // The slider handlers of the Decky panel: every edit leaves a valid FPPT >= SPPT >= SPL draft.
    function editPower(key,value) {
        powerNotice="";
        const caps=customCaps();
        powerDraft=key==="sppt"?Tdp.withSppt(powerDraft,value,caps):key==="fppt"?Tdp.withFppt(powerDraft,value,caps):
            tdpAdvanced()?Tdp.withSpl(powerDraft,value,caps,tdpMin()):Tdp.simpleTuning(value,caps,tdpMin());
        powerDirty=true; powerPreset="custom"; customPicked=true;
    }
    function applyPower() {
        if (!profileReady()) { backend.error="Wait for a fresh game profile before applying this draft."; return; }
        if(tdpScope!=="global" && app()!==draftGame) { backend.error="The running game changed. Reload this profile before applying the draft."; return; }
        const a=Tdp.absolute(powerPreset==="custom" && !tdpAdvanced()?Tdp.simpleTuning(powerDraft.spl,customCaps(),tdpMin()):powerDraft);
        if(tdpScope==="ac") change("set_game_ac_profile",[app(),Math.round(a.spl*1000),Math.round(a.sppt*1000),Math.round(a.fppt*1000),acSeparate,powerPreset,stored()]);
        else change("apply_tdp",[Math.round(a.spl*1000),Math.round(a.sppt*1000),Math.round(a.fppt*1000),powerTarget(),powerPreset,expectedGame(),storedEdit()]);
    }
    function capsNow() { return Tdp.capsState(s("get_caps")); }
    function unlocked() { return s("get_extras_unlocked")===true; }
    function stdCaps() { const std=s("get_caps").std;return std?{spl:Number(std.spl),sppt:Number(std.sppt),fppt:Number(std.fppt)}:Tdp.FALLBACK_STD; }
    // As in Decky: who sets the limits that run now, named by their values. A saved profile being edited
    // is not the running game's, so then the values tell whether Global runs.
    function tdpSummary() {
        const settings=s("get_settings"),now=Tdp.activeLimits(settings,presetTable());
        if(settings.enabled===false)return Tdp.powerSummary(false);
        if(settings.enabled!==true || !now)return "";
        const profile=stored()?null:gameProfile(),own=profile?!!profile.exists && profile.active!==false:!now.global;
        const info=s("get_tdp_info");
        return Tdp.powerSummary(true,own?gameTitle(running()):"Global",now.label,now,info.success?info.values?.package_draw:null,unlocked(),stdCaps());
    }
    function setExtras(value) {
        if(!value) { change("set_extras_unlocked",[false]); return; }
        const ask=Tdp.extrasConfirm(capsNow());
        confirm("set_extras_unlocked",[true],ask.text,ask.title,ask.ok);
    }
    function tdpNoticeText() { const notice=Tdp.tdpNotice(capsNow(),unlocked());return notice?notice[0]+". "+notice[1]:""; }
    // Decky's "Could not load profile": the game's profile read failed; the editor stays locked until it loads.
    function profileReadError() { return page==="tdp" && !!app()?backend.readErrors?.get_game_profile || "":""; }
    function cpuShown() { return Tdp.editorControls(s("get_cpu_power_controls")) || {}; }
    function vibeArgs(value) { const state=s("vibe_get_settings"); return [value,state.app_id,state.profile_id]; }
    function rgbSettings() { return s("rgb_get_status").settings || {}; }
    function rgbUsesColor() { return ["monocolor","breathe"].indexOf(rgbSettings().effect)>=0; }
    function rgbSpeed() { const speed=Number(rgbSettings().speed);return Number.isFinite(speed)?[0,50,100].reduce((a,b)=>Math.abs(b-speed)<Math.abs(a-speed)?b:a):undefined; }
    readonly property var moduleText:({
        tdp:["TDP","Power limits and CPU settings. Off: Lenovo's default limits return."],
        fan:["Fan Control","Your own fan curve. Off: the fan returns to automatic."],
        vibration:["Vibration","Controller and touchpad vibration."],
        rgb:["Lighting","Stick lights and power button light."],
        remap:["Button mapping","The two extra buttons on the left controller."],
        controller:["Gyro & touchpad","Gyro source and controller test."],
        battery:["Battery","Charge limit of about 80%."],
        display:["Display","OLED brightness and HDR fixes. Changes need a Gaming Mode restart."],
        wifi:["Wi-Fi","Wi-Fi band preference."]})
    function moduleFields(name) {
        const label=moduleText[name][0];
        const entry=()=>s("get_version").modules?.[name] || {};
        return [
            switcher(label,()=>entry().enabled,"modules_set_enabled",v=>[name,v],{
                set:v=>{ if(!backend.busy && backend.connected)applyingModule=name; change("modules_set_enabled",[name,v]); },
                available:()=>!entry().maintenance && !entry().pending,
                note:()=>applyingModule===name?"Applying…":entry().error || entry().note || (entry().pending?"Restoration is pending.":entry().enabled===false?"":moduleText[name][1])}),
            action("Retry "+label+" cleanup","modules_set_enabled",()=>[name,false],{
                visible:()=>!!entry().pending && !entry().maintenance}),
            action("Retry "+label+" start","modules_set_enabled",()=>[name,true],{
                visible:()=>entry().enabled===true && entry().ready===false && !entry().pending && !entry().maintenance})
        ];
    }
    // The Decky scale in 1% steps (Decky moves in 10% steps).
    function eppPercent() { return Tdp.eppPercent(cpuShown().epp,1); }
    function fieldEnabled(owner,entry) {
        if(entry.kind==="text" || entry.kind==="paragraph")return true;
        if(owner==="rgb" && s("rgb_get_status").recovery_required)return false;
        const gate=s("get_version");
        return !!gate && typeof gate==="object" && !Array.isArray(gate) && gate.blocked===false &&
            backend.age("get_version")<8 && backend.connected && !backend.busy &&
            (page!=="tdp" || profileReady() || !!entry.scope) &&
            (owner==="about" || owner==="modules" || owner==="firmware" || !!gate.modules?.[owner]?.ready) && entry.available();
    }
    // Readable controller test rows instead of raw JSON (UX spec 4.5).
    function onOff(value) { return value===true?"On":value===false?"Off":"—"; }
    function testRunning() { return !!backend.diagnosticToken; }
    function testSample(stream) { return s("controller_get_diagnostics")[stream]?.sample || {}; }
    function vectorText(value) { return value?["x","y","z"].map(axis=>axis.toUpperCase()+" "+(Number.isFinite(value[axis])?value[axis]:"—")).join(" · "):"—"; }
    function touchText(value) { return !value || typeof value.is_touching!=="boolean"?"—":value.is_touching?"touch at "+Number(value.x).toFixed(2)+","+Number(value.y).toFixed(2):"no touch"; }
    function streamText(name) {
        const stream=s("controller_get_diagnostics")[name] || {};
        return !stream.received?"No reports yet.":stream.reports+" reports · "+(Number.isFinite(stream.rate_hz)?Number(stream.rate_hz).toFixed(0)+" Hz":"rate unknown");
    }
    readonly property var displayModes:({hybrid:"Hybrid",pq:"PQ",gamma22:"Gamma 2.2"})
    // Same one upside and one downside per mode as the Decky panel.
    readonly property var displayTradeoffs:({
        hybrid:"+ Gamma 2.2 for everyday use and PQ only while a game is presenting HDR, so neither one is given up.\n− Each switch between HDR and SDR briefly blanks the screen.",
        pq:"+ HDR reaches the panel's full 1100 nit peak, at the absolute levels the content was mastered for.\n− The panel ignores its brightness control, so dimming only fades the image and shadows wash out to grey.",
        gamma22:"+ The system brightness slider drives the panel directly, so black stays black at any level.\n− HDR is tone mapped against the panel's ~471 nit ceiling, so bright highlights lose their punch."})
    function displayModeNow() {
        const state=s("display_get_state"),mode=state.panel_mode;
        return mode==="hybrid"?"Now: Hybrid"+(state.gamescope_reachable?" (screen in "+(state.hdr_now?"PQ":"Gamma 2.2")+")":""):"Now: "+(displayModes[mode] || pretty(mode));
    }
    function displayRestartPending() { return !!display().restart_pending || !!display().reset_restart_pending; }
    // First run: every mode with its trade-off, Hybrid recommended; later: only the other two.
    function displayModeChoices() {
        const current=display().panel_mode;
        return ["hybrid","pq","gamma22"].filter(id=>id!==current).map(id=>({id:id,detail:displayTradeoffs[id],
            label:current?"Use "+displayModes[id]:id==="hybrid"?"Hybrid (recommended)":displayModes[id]}));
    }
    function confirmDisplaySetup(mode) {
        confirm("display_run_setup",[mode],"If you already have a display script for this screen, it's kept as a .backup file. Gaming Mode needs a restart afterwards.","Install display fix for "+(displayModes[mode] || pretty(mode))+"?","Install");
    }
    function pickDisplayMode(mode) { displaySwitch=false; if(!display().panel_mode)confirmDisplaySetup(mode);else change("display_set_panel_mode",[mode]); }
    function restartGamingMode() { confirm("display_restart_session",[],"Steam and any running game will close and reopen. Save your game first.","Restart Gaming Mode?","Restart"); }
    function wifi() { return s("wifi_get_status"); }
    // Like Decky: check the action against the status shown, then verify it in the next status read.
    function wifiRun(action,method,args) {
        if(!Wifi.canRun(wifi(),action)) { backend.error=Wifi.CHANGED; return; }
        wifiCheck={action:action,before:wifi(),done:false};
        change(method,args);
    }
    function setWifiPolicy(value) {
        const action=Wifi.POLICY_ACTIONS[value],ask=Wifi.CONFIRMATIONS[value];
        if(value===Wifi.savedPolicy(wifi()) || !Wifi.canRun(wifi(),action))return;
        if(ask)confirmAction(ask.text,ask.title,ask.ok,()=>wifiRun(action,"wifi_set_band_policy",[value,value==="five_six_only"]));
        else wifiRun(action,"wifi_set_band_policy",[value,false]);
    }
    function forms() {
        const vibeReady=()=>!!s("vibe_is_ready").ready;
        const cpu=()=>s("get_cpu_power_controls");
        // Charger CPU settings are saved into the game's charger split, so they wait until it exists.
        const cpuScopeReady=()=>String(cpu().profile?.app_id || "")===powerTarget() && !!cpu().profile?.ac_profile===(tdpScope==="ac") &&
            (tdpScope!=="ac" || gameProfile().ac_separate===true);
        const gyro=()=>s("controller_get_status");
        const battery=()=>s("battery_get_status");
        return {
            tdp:[{title:"Power",column:0,fields:[
                notice("",tdpNoticeText),
                notice("Could not load profile",profileReadError),
                field("Retry profile","button",()=>"Retry profile",()=>backend.issue("get_game_profile",[app()]),{visible:()=>profileReadError().length>0,scope:true}),
                field("Editing a saved profile","button",()=>"Done",()=>editProfile(running()),{visible:stored,
                    note:()=>gameTitle(editGame)+" isn't running. Changes are saved for it and apply when it starts."}),
                switcher("Enable TDP control",()=>s("get_settings").enabled,"set_plugin_enabled",null,{available:()=>!!s("get_caps").tdp_available,note:tdpSummary}),
                choices("Settings for this game",gameUse,chooseUse,()=>[{id:"global",label:"Global"},{id:"game",label:"This game"},{id:"charger",label:"This game + charger"}],
                    {kind:"options",scope:true,visible:()=>!!app() && s("get_settings").enabled===true,available:gameProfileFresh,
                     note:()=>Tdp.scopeNote(gameUse(),gameTitle(app()))}),
                choices("Edit settings for",()=>tdpScope==="ac"?"charger":"battery",chooseEditTarget,()=>[{id:"battery",label:"Battery"},{id:"charger",label:"Charger"}],
                    {kind:"options",scope:true,visible:()=>!!app() && s("get_settings").enabled===true && gameUse()==="charger",available:gameProfileFresh,note:editTargetNote}),
                choices("Preset",()=>powerPreset,choosePreset,powerOptions,{kind:"options",available:()=>!!s("get_caps").tdp_available && !!s("get_settings").enabled,
                    note:()=>Tdp.presetNote(powerPreset,presetTable())}),
                number("Sustained power",()=>powerDraft.spl,v=>editPower("spl",v),tdpMin,()=>customCaps().spl,{kind:"range",suffix:" W",visible:()=>powerPreset==="custom",
                    note:()=>tdpAdvanced()?"":"Boost follows automatically."}),
                number("Slow boost",()=>powerDraft.spptOff,v=>editPower("sppt",v),0,()=>boostRoom().sppt || 1,{kind:"range",prefix:"+",suffix:" W",visible:()=>powerPreset==="custom" && tdpAdvanced(),
                    available:()=>boostRoom().sppt>0,note:()=>boostNote(powerDraft.spptOff,boostRoom().sppt)}),
                number("Fast boost",()=>powerDraft.fpptOff,v=>editPower("fppt",v),0,()=>boostRoom().fppt || 1,{kind:"range",prefix:"+",suffix:" W",visible:()=>powerPreset==="custom" && tdpAdvanced(),
                    available:()=>boostRoom().fppt>0,note:()=>boostNote(powerDraft.fpptOff,boostRoom().fppt)}),
                field("","buttons",()=>powerDirty?"Unsaved changes":"",id=>{ if(id==="apply")applyPower(); else { powerDirty=false; customPicked=powerPreset==="custom"; loadPower(); } },
                    {name:"Custom limits",visible:()=>powerPreset==="custom",available:()=>profileReady() && !!s("get_caps").tdp_available && !!s("get_settings").enabled,
                     choices:()=>[{id:"apply",label:applyLabel(),tone:"primary",enabled:powerDirty},{id:"discard",label:"Discard",enabled:powerDirty}]}),
                text("Limits now",()=>Tdp.limitsText(s("get_tdp_info"))),
                text("Set by",()=>Tdp.sourceText(s("get_tdp_info"))),
                text("APU power",()=>{
                    const value=s("get_tdp_info").values?.package_draw;
                    return value===undefined || value===null?"—":Number(value).toFixed(1)+" W";
                }),
                notice("Power profile",()=>powerNotice)
            ]},{title:"CPU",column:1,fields:[
                // Each UI keeps its control style (Decky: a 10% slider, or a slider over the named profiles plus "Use …" buttons).
                switcher("CPU Boost",()=>cpuShown().cpu_boost?.enabled,"set_cpu_boost",v=>[v,powerTarget(),tdpScope==="ac",expectedGame(),storedEdit()],{available:()=>cpuScopeReady() && Tdp.boostUsable(cpuShown().cpu_boost),
                    note:()=>Tdp.boostNote(cpuShown().cpu_boost)}),
                number("CPU energy preference",eppPercent,v=>change("set_epp",[Tdp.eppValue(v),powerTarget(),tdpScope==="ac",expectedGame(),storedEdit()]),0,100,{kind:"range",suffix:"%",lowLabel:Tdp.eppLabel("performance"),highLabel:Tdp.eppLabel("power"),note:()=>Tdp.eppNote(cpuShown().epp),visible:()=>!!cpu().epp?.numeric_supported,available:()=>cpuScopeReady() && !!cpu().epp?.available}),
                choices("CPU energy preference",()=>cpuShown().epp?.value,v=>change("set_epp",[v,powerTarget(),tdpScope==="ac",expectedGame(),storedEdit()]),()=>items(cpu().epp?.profiles).map(p=>({id:p,label:Tdp.eppLabel(p)})),{name:"CPU energy profile",kind:"options",visible:()=>!cpu().epp?.numeric_supported,available:()=>cpuScopeReady() && !!cpu().epp?.available,
                    note:()=>cpu().epp?.available?"":Tdp.eppUnavailable(cpu().epp)}),
                notice("",()=>cpu().error || cpu().epp?.compatibility_note || "")
            ]},{title:"Game profiles",column:1,note:"Games with their own settings. Edit opens a game's settings above, also when it isn't running.",fields:[
                field("","list",profileRows,profileAction,{name:"Game profiles"})
            ]},{title:"More",column:1,barTitle:"Troubleshooting",fields:[
                switcher("Separate boost limits",()=>s("get_settings").advanced_tdp_control,"set_advanced_tdp_control",null,{note:"Set slow and fast boost yourself in Custom."}),
                field("Allow up to 50 W","toggle",unlocked,setExtras,{note:()=>Tdp.extrasNote(unlocked(),capsNow(),stdCaps().spl),visible:()=>Tdp.extrasShown(capsNow())}),
                action("Reapply saved settings","reapply",null,{bar:true}),
                action("Retry helper download","retry_extras",null,{bar:true,visible:()=>Tdp.helperRetryShown(capsNow(),unlocked())}),
                field("Delete profile for this game","button",()=>"Delete profile for this game",()=>confirm("delete_game_profile",[app()],"Its power, CPU Boost and energy settings are removed. This game will use Global settings.","Delete profile for this game?","Delete",true),
                    {bar:true,danger:true,visible:()=>!!gameProfile().exists})
            ]}],
            vibration:[{title:"Vibration",barTitle:"Troubleshooting",fields:[
                notice("",()=>s("vibe_is_ready").error || ""),
                switcher("Per-game profile",()=>s("vibe_get_settings").overwrite,"vibe_set_profile_overwrite",v=>[app(),v,""],{available:()=>vibeReady() && !!app(),
                    note:()=>!app()?"Start a game to give it its own settings.":s("vibe_get_settings").overwrite?"This game has its own settings.":"This game uses the global settings."}),
                choices("Vibration strength",()=>s("vibe_get_settings").settings?.level,v=>change("vibe_set_intensity",vibeArgs(v)),()=>indexedOptions(s("vibe_get_capabilities").intensity),{kind:"options",available:vibeReady}),
                choices("Vibration mode",()=>s("vibe_get_settings").settings?.mode,v=>change("vibe_set_rumble_mode",vibeArgs(v)),()=>indexedOptions(s("vibe_get_capabilities").mode),{kind:"options",note:"A vibration pattern tuned for a game type.",available:vibeReady}),
                switcher("Touchpad vibration",()=>s("vibe_get_settings").settings?.touchpadEnabled,"vibe_set_touchpad_enabled",vibeArgs,{available:vibeReady}),
                choices("Touchpad strength",()=>s("vibe_get_settings").settings?.touchpadIntensity,v=>change("vibe_set_touchpad_intensity",vibeArgs(v)),()=>indexedOptions(s("vibe_get_capabilities").tp_intensity),{kind:"options",available:vibeReady}),
                action("Test vibration","vibe_test_vibration",()=>[500],{note:"A short buzz with the current settings.",available:vibeReady}),
                action("Reset to defaults","vibe_reset_to_default",()=>[s("vibe_get_settings").app_id,s("vibe_get_settings").profile_id],{available:vibeReady}),
                action("Reapply settings","vibe_reapply",null,{bar:true,available:vibeReady})
            ]}],
            rgb:[{title:"Lighting",fields:[
                notice("",()=>s("rgb_get_status").recovery_required?"Lighting is restoring its last state. Controls unlock when it finishes.":""),
                switcher("Enable lighting control",()=>rgbSettings().control_enabled,"rgb_set_control_enabled",null,{available:()=>!!s("rgb_get_status").rgb?.supported,note:"Companion sets the stick lights and restores them after sleep."}),
                switcher("Stick lights",()=>rgbSettings().rings_enabled,"rgb_set_rings_enabled",null,{available:()=>!!rgbSettings().control_enabled}),
                choices("Effect",()=>rgbSettings().effect,v=>change("rgb_set_effect",[v]),()=>s("rgb_get_status").rgb?.effects || [],{kind:"options",available:()=>!!rgbSettings().control_enabled}),
                number("Hue",()=>rgbSettings().hue,v=>change("rgb_set_color",[v,Number(rgbSettings().saturation || 0)]),0,359,{kind:"range",hue:true,suffix:"°",visible:rgbUsesColor,available:()=>!!rgbSettings().control_enabled}),
                number("Saturation",()=>rgbSettings().saturation,v=>change("rgb_set_color",[Number(rgbSettings().hue || 0),v]),0,100,{kind:"range",suffix:"%",visible:rgbUsesColor,available:()=>!!rgbSettings().control_enabled}),
                number("Brightness",()=>rgbSettings().brightness,v=>change("rgb_set_brightness",[v]),0,100,{kind:"range",suffix:"%",available:()=>!!rgbSettings().control_enabled}),
                choices("Speed",rgbSpeed,v=>change("rgb_set_speed",[v]),()=>[{id:0,label:"Slow"},{id:50,label:"Medium"},{id:100,label:"Fast"}],{kind:"options",visible:()=>rgbSettings().effect!=="monocolor",available:()=>!!rgbSettings().control_enabled}),
                // As in Decky: the saved choice only once Companion manages the light, otherwise what the light shows.
                switcher("Power button light",()=>rgbSettings().power_led_managed?rgbSettings().power_led_enabled:s("rgb_get_status").power_led?.current ?? true,"rgb_set_power_led",null,{available:()=>!!s("rgb_get_status").power_led?.supported,
                    note:()=>"Turn the light in the power button on or off."+(s("rgb_get_status").power_led?.drift?" Restoring saved state…":"")}),
                action("Restore original lighting","rgb_restore_original",null,{note:"Returns the lights to how they were before Companion."}),
                notice("",()=>s("rgb_get_status").error || (s("rgb_get_status").power_led?.supported===false?s("rgb_get_status").power_led?.reason || "":""))
            ]}],
            remap:[{title:"Button mapping",fields:[
                notice("",()=>s("remap_get_status").supported===false?"Button mapping unavailable. "+(s("remap_get_status").reason || ""):s("remap_get_status").error || ""),
                switcher("Enable button mapping",()=>s("remap_get_status").enabled,"remap_set_enabled",null,{available:()=>!!s("remap_get_status").supported,
                    note:()=>"Change what the two extra buttons on the left controller do."+(s("remap_get_status").enabled && s("remap_get_status").active===false?" Waiting to reapply…":"")}),
                choices("Desktop button",()=>s("remap_get_status").desktop_action,v=>change("remap_set_action",["desktop",v]),()=>s("remap_get_status").actions || [],{note:"Default: SteamOS behaviour."}),
                choices("Page button",()=>s("remap_get_status").page_action,v=>change("remap_set_action",["page",v]),()=>s("remap_get_status").actions || [],{note:"Default: takes a screenshot."}),
                action("Restore defaults","remap_restore_defaults")
            ]}],
            controller:[{title:"Gyro & touchpad",column:1,barTitle:"Troubleshooting",fields:[
                notice("",()=>gyro().recovery_pending?"A controller change didn't finish. Restore the previous settings first.":gyro().error || (gyro().available===false || gyro().conflict?gyro().reason || "":"")),
                choices("Gyro source",()=>gyro().gyro_source,v=>change("controller_set_gyro_source",[v]),()=>[{id:"system",label:"System default"},{id:"combined",label:"Both controllers"},{id:"left",label:"Left controller"},{id:"right",label:"Right controller"}],
                    {kind:"options",available:()=>!!gyro().available && !gyro().conflict,
                     note:()=>"Which controller's motion Steam uses. System default hands control back to SteamOS."+(gyro().gyro_source && gyro().gyro_source!=="system" && gyro().applied_source!==gyro().gyro_source?" Not confirmed yet…":"")}),
                field("Controller test","button",()=>backend.diagnosticToken?"Stop test · "+Math.max(0,Math.ceil(Number(s("controller_get_diagnostics").remaining_s ?? 30)))+" s left":"Start controller test",
                    ()=>backend.diagnosticToken?change("controller_stop_diagnostics",[backend.diagnosticToken]):change("controller_start_diagnostics"),
                    // The test only reads the physical controller, so it does not need InputPlumber (gyro().available).
                    {note:"Move the controllers and touch the touchpad for 30 s to see live readings.",available:()=>!!backend.diagnosticToken || !!gyro().physical}),
                text("Left gyro",()=>vectorText(testSample("physical").gyro_left),{visible:testRunning,note:()=>streamText("physical")}),
                text("Right gyro",()=>vectorText(testSample("physical").gyro_right),{visible:testRunning}),
                text("Steam gyro",()=>vectorText(testSample("virtual").gyro),{visible:testRunning,note:()=>streamText("virtual")}),
                text("Touchpad",()=>"Physical: "+touchText(testSample("physical").touchpad)+" · Steam: "+touchText(testSample("virtual").touchpad),{visible:testRunning}),
                field("","button",()=>controllerDetails?"Hide details":"Show details",()=>{ controllerDetails=!controllerDetails; },{name:"Details"}),
                text("Physical controller",()=>gyro().physical?.path || "—",{visible:()=>controllerDetails}),
                text("Steam controller",()=>gyro().virtual?.path || "—",{visible:()=>controllerDetails}),
                text("Gyro reporting",()=>{ const actual=gyro().imu?.actual;return actual?"left "+onOff(actual.left)+" · right "+onOff(actual.right):"—"; },{visible:()=>controllerDetails}),
                action("Restore previous controller settings","controller_release_control",null,{bar:true,visible:()=>!!gyro().recovery_pending || !!gyro().conflict})
            ]},{title:"Controller firmware",owner:"firmware",column:0,fields:[
                notice("",firmwareMessage),
                text("Status",()=>firmwarePhase()),
                text("Left controller",()=>firmwareRow("left"),{visible:()=>firmwareRowVisible("left")}),
                text("Right controller",()=>firmwareRow("right"),{visible:()=>firmwareRowVisible("right")}),
                text("Receiver",()=>firmwareRow("receiver"),{visible:()=>firmwareRowVisible("receiver")}),
                notice("Install unavailable",firmwareInstallReason),
                field("Firmware update","button",firmwareActionLabel,firmwareAct,{available:firmwareActionAvailable})
            ]}],
            battery:[{title:"Battery",barTitle:"Troubleshooting",fields:[
                notice("",()=>battery().recovery_pending?"A charging change didn't finish. Restore the previous charging mode before changing it again.":battery().error || (battery().supported===false?battery().reason || "":"")),
                text("Battery",()=>battery().capacity===undefined || battery().capacity===null?"—":percent(battery().capacity)+" · "+(battery().charging_status || "Unknown")),
                text("Controllers",()=>"left "+percent(s("battery_get_controller_levels").left)+" · right "+percent(s("battery_get_controller_levels").right)),
                switcher("Battery protection",()=>battery().enabled,"battery_set_enabled",null,{available:()=>!!battery().supported && !battery().recovery_pending,
                    note:()=>"Stops charging at about 80% to protect the battery."+(typeof battery().enabled!=="boolean"?" Current state unknown.":
                        battery().managed && typeof battery().requested_enabled==="boolean" && battery().requested_enabled!==battery().enabled?" Waiting for the battery to confirm…":"")}),
                notice("",()=>battery().current_mode?"Charging mode reported: "+battery().current_mode:""),
                action("Restore previous charging mode","battery_release_control",null,{bar:true,visible:()=>!!battery().managed || !!battery().recovery_pending})
            ]}],
            display:[
                {title:"Display",column:0,visible:()=>nativeDisplay(),fields:[
                    notice("",()=>confirmedNativeDisplay()?"SteamOS supports this screen. Companion's display fix is off. The HDR game fix below still works.":display().native_support_reason || "Native display support was detected earlier. Waiting for confirmation."),
                    notice("",()=>display().native_cleanup_error || (display().native_cleanup_pending?"Removing Companion's earlier display changes.":"")),
                    field("Restart Gaming Mode","button",()=>"Restart Gaming Mode",restartGamingMode,{visible:()=>!!display().native_restart_pending && !!s("desktop_context").gamescope}),
                    notice("",()=>display().native_restart_pending && !s("desktop_context").gamescope?"Takes effect when you return to Gaming Mode.":"")
                ]},
                {title:"Display fix",column:0,visible:()=>!nativeDisplay(),barTitle:"Troubleshooting",fields:[
                    notice("",()=>display().setup_error || ""),
                    field("Restart Gaming Mode","button",()=>"Restart Gaming Mode",restartGamingMode,{visible:()=>displayRestartPending() && !!s("desktop_context").gamescope,
                        note:()=>display().restart_error?display().restart_error+". Restart from the Steam menu (Power → Switch to Desktop and back) or reboot.":"Steam closes and reopens."}),
                    notice("",()=>displayRestartPending() && !s("desktop_context").gamescope?"Takes effect when you return to Gaming Mode.":""),
                    // As in Decky: the mode is a decision already made, so its alternatives stay folded away.
                    field("","button",()=>displaySwitch?"Cancel":"Change display mode",()=>{ displaySwitch=!displaySwitch; },{name:"Change display mode",note:displayModeNow,visible:()=>!!display().panel_mode && !!display().setup_done}),
                    choices("Display mode",()=>display().panel_mode,pickDisplayMode,displayModeChoices,{kind:"options",visible:()=>!display().panel_mode || (displaySwitch && !!display().setup_done),
                        note:()=>display().panel_mode?"":"Choose one. You can change it later."}),
                    field("Install display fix","button",()=>"Install display fix",()=>confirmDisplaySetup(display().panel_mode || "hybrid"),{visible:()=>!!display().panel_mode && !display().setup_done,
                        note:()=>"Display script needed ("+(displayModes[display().panel_mode] || pretty(display().panel_mode))+"). "+(display().setup_note || "Install it to use this mode.")}),
                    switcher("Brightness slider in HDR",()=>display().enabled,"display_set_enabled",null,{available:()=>!!display().setup_done,visible:()=>!!display().setup_done,
                        note:()=>"Lets the Steam brightness slider dim the screen in PQ."+(!display().enabled?"":display().active?" Working now ("+watts(display().nits)+" nits).":" Not needed in the current mode.")}),
                    field("","button",()=>displayDetails?"Hide details":"Show details",()=>{ displayDetails=!displayDetails; },{name:"Display details"}),
                    text("Panel",()=>display().panel_desc || "—",{visible:()=>displayDetails}),
                    // What the brightness forwarding is doing; "0 nits" while nothing is forwarded would mislead.
                    text("Brightness",()=>display().reason || "—",{visible:()=>displayDetails}),
                    field("Reset display fix","button",()=>"Reset display fix",()=>confirm("display_reset_settings",[],"Companion removes its display changes and restores your previous display script, if there was one. Gaming Mode may need a restart.","Reset display fix?","Reset",true),{bar:true,danger:true})
                ]},
                {title:"HDR info for games",column:1,fields:[
                    switcher("Fix HDR brightness in games",()=>display().edid_fix,"display_set_edid_fix",null,{note:"Games see the screen's real HDR brightness. Restart the game after changing."}),
                    text("Status",()=>!display().edid_fix?"Off":display().edid_patched && Number(display().edid_game_nits)>0?"On · games see "+watts(display().edid_game_nits)+" nits":display().edid_patched?"On":"Not active yet"),
                    notice("",()=>display().edid_reason || "")
                ]}
            ],
            wifi:[{title:"Wi-Fi",fields:[
                notice("",()=>{const notice=Wifi.wifiNotice(wifi());return wifi().error || wifi().message || (notice?notice[0]+". "+notice[1]:"");}),
                text("Network",()=>Wifi.networkName(wifi()),{note:()=>Wifi.linkText(wifi())}),
                choices("Band",()=>Wifi.savedPolicy(wifi()),setWifiPolicy,()=>Wifi.bandOptions(wifi()),
                    {kind:"options",note:()=>Wifi.bandNote(wifi()),visible:()=>Wifi.actionShown(wifi(),"band"),available:()=>Wifi.bandOptions(wifi()).length>1}),
                field("Reconnect to 5/6 GHz","button",()=>"Reconnect",()=>wifiRun("high","wifi_rescan_and_reconnect",["high"]),
                    {visible:()=>Wifi.actionShown(wifi(),"high"),available:()=>Wifi.canRun(wifi(),"high")}),
                field("Connect to 6 GHz once","button",()=>"Connect",()=>wifiRun("six","wifi_rescan_and_reconnect",["six"]),
                    {note:"Then roams normally again.",visible:()=>Wifi.actionShown(wifi(),"six"),available:()=>Wifi.canRun(wifi(),"six")}),
                text("Backend",()=>Wifi.backendName(wifi())),
                text("Driver",()=>Wifi.driverText(wifi())),
                text("Frequency",()=>Wifi.frequencyText(wifi()))
            ]}],
            modules:[{title:"Choose your modules",note:"Turning a module off stops it. Its settings are kept.",fields:["tdp","fan","vibration","rgb","remap","controller","battery","display","wifi"].reduce((fields,name)=>fields.concat(moduleFields(name)),[]).concat([
                notice("",()=>s("get_version").modules?.display?.note?"Takes effect when you return to Gaming Mode.":"")])}],
            about:[{title:"Companion",fields:[
                text("Version",()=>s("get_version").version || "—"),
                text("Latest version",()=>s("updates_check").latest_version || "Not checked"),
                action("Check for updates","updates_check"),
                field("Download update","button",()=>"Download "+(s("updates_check").latest_version || ""),()=>change("updates_download",[s("updates_check").latest_version]),{visible:()=>!!s("updates_check").update_available && !!s("updates_check").download_available && !!s("updates_check").latest_version}),
                text("Downloaded to",()=>s("updates_download").path || "—",{visible:()=>!!s("updates_download").path}),
                notice("",()=>s("updates_download").path?(s("updates_download").version || "Update")+" downloaded. Install it from Decky settings → Developer → Install plugin from ZIP.":"")
            ]}]
        };
    }
    function display() { return s("display_get_state"); }
    function nativeDisplay() { const state=s("display_get_state");return state.native_support_detected===true || state.native_cleanup_pending===true; }
    function confirmedNativeDisplay() { const state=s("display_get_state");return state.native_support_detected===true && String(state.native_support_reason || "").indexOf("Gamescope includes the native Legion Go 2 display profile.")===0; }
    // Firmware keeps its single action button in a row below its readouts.
    function inBar(card,entry) { return entry.kind==="button" && (!!entry.bar || card.owner==="firmware"); }
    function rebuild() {
        const sections=forms();
        const owners=page==="controller"?["vibration","remap","controller"]:[page];
        let merged=[];
        for(const owner of owners) merged=merged.concat((sections[owner] || []).map(card=>Object.assign({owner:owner},card)));
        cards=merged.map((card,index)=>Object.assign({column:index%2},card));
    }
    onPageChanged: { if(editGame) { editGame=""; backend.editApp=""; } tdpScope="global"; powerDirty=false; customPicked=false; controllerDetails=false; displaySwitch=false; displayDetails=false; wifiCheck=null; loadPower(); rebuild(); }
    Component.onCompleted: { loadPower(); rebuild(); }
    Timer { interval:500; repeat:true; running:backend.windowActive; onTriggered:panel.tick=backend.now() }
    Connections {
        target:panel.backend
        function onChanged(method) {
            // A deleted profile ends its editing.
            if(method==="list_game_profiles" && panel.editGame && !panel.items(panel.s(method).profiles).some(p=>String(p.app_id)===panel.editGame)) {
                panel.powerDirty=false; panel.editProfile(panel.running());
            }
            if(method==="get_game_profile" || method==="desktop_context") panel.syncScope();
            if(method==="get_settings" || method==="get_game_profile") panel.loadPower();
            if(method==="desktop_context" && panel.page==="tdp") { backend.cpuApp=panel.powerTarget(); backend.cpuAc=panel.tdpScope==="ac"; }
            if(method==="wifi_get_status" && panel.wifiCheck?.done) {
                if(!Wifi.operationVerified(panel.wifiCheck.action,panel.wifiCheck.before,panel.wifi()))backend.error=Wifi.UNVERIFIED;
                panel.wifiCheck=null;
            }
        }
        // Like Decky while hidden: a read after the window was away may show a later, unrelated change.
        function onWindowActiveChanged() { if(!backend.windowActive)panel.wifiCheck=null; }
        function onMutationDone(method,result,success) {
            if(method==="modules_set_enabled")panel.applyingModule="";
            if(method.startsWith("wifi_") && panel.wifiCheck)panel.wifiCheck=success && result?.success!==false?Object.assign({},panel.wifiCheck,{done:true}):null;
            // Switching the boost model drops the draft, as in Decky.
            if(method==="set_advanced_tdp_control" && success) { panel.powerDirty=false; panel.loadPower(); }
            if(method==="apply_tdp" || method==="set_game_ac_profile") {
                if(success) { panel.powerDirty=false; panel.customPicked=false; }
                panel.powerNotice=success && result?.stored ? "Saved for "+panel.gameTitle(panel.editGame)+". It applies when the game starts." :
                    success && result?.saved_only ? "Saved for Global. It applies when no game profile is active." : "";
            }
        }
    }
    Repeater {
        model:panel.columns
        delegate:ColumnLayout {
        id:cardColumn
        required property int index
        Layout.fillWidth:true; Layout.minimumWidth:0; Layout.preferredWidth:1; Layout.alignment:Qt.AlignTop
        Layout.maximumWidth:panel.cards.length<2?theme.formWidth:Number.POSITIVE_INFINITY
        spacing:theme.gap
        Repeater {
        model:panel.cards.filter(card=>(panel.columns===1?0:card.column)===cardColumn.index)
        delegate:UiCard {
            id:section
            required property var modelData
            objectName:"moduleSection_"+modelData.owner
            visible:!modelData.visible || modelData.visible()
            title:modelData.title; icon:modelData.owner
            readonly property bool gated:["about","modules","firmware"].indexOf(modelData.owner)<0
            readonly property bool open:!section.gated || moduleStatus.ready
            readonly property var barFields:section.modelData.fields.filter(f=>panel.inBar(section.modelData,f))
            readonly property bool barShown:section.open && section.barFields.some(f=>!f.visible || f.visible())
            ModuleStatus { id:moduleStatus;backend:panel.backend;owner:section.modelData.owner;tick:panel.tick }
            Layout.fillWidth:true; Layout.alignment:Qt.AlignTop; Layout.minimumWidth:0
            function preview(label,fallback) { for(let i=0;i<fields.count;i++){const item=fields.itemAt(i);if(item && item.label===label)return item.liveValue;}return fallback; }
            trailing: [
                Row {
                    spacing:6; visible:moduleStatus.ready && section.modelData.owner==="rgb" && panel.rgbUsesColor()
                    Accessible.name:"Color preview"
                    Repeater { model:2;Rectangle { width:26;height:26;radius:13;color:"transparent";border.width:4;border.color:panel.rgbUsesColor()?Qt.hsva(section.preview("Hue",0)/360,section.preview("Saturation",100)/100,section.preview("Brightness",100)/100,1):theme.accentText } }
                },
                UiBadge { visible:section.gated && !moduleStatus.ready;text:moduleStatus.label;tone:["disabled","offline","loading"].includes(moduleStatus.state)?"neutral":"amber" }
            ]
            Rectangle {
                visible:section.modelData.owner==="firmware"; Layout.fillWidth:true
                implicitHeight:firmwareWarning.implicitHeight+24; radius:8
                color:Qt.rgba(theme.danger.r,theme.danger.g,theme.danger.b,0.10)
                border.color:Qt.rgba(theme.danger.r,theme.danger.g,theme.danger.b,0.45)
                Label {
                    id:firmwareWarning; anchors { left:parent.left; right:parent.right; verticalCenter:parent.verticalCenter; margins:12 }
                    textFormat:Text.PlainText; text:"Experimental feature. Use at your own risk."
                    color:theme.danger; font.pixelSize:13; font.weight:Font.DemiBold; wrapMode:Text.WordWrap
                }
            }
            Label { textFormat:Text.PlainText; text:modelData.note || ""; visible:text.length>0 && section.open; wrapMode:Text.WordWrap; color:theme.muted; font.pixelSize:theme.fontSmall; Layout.fillWidth:true }
            UiModuleNotice { Layout.fillWidth:true;visible:section.gated && !moduleStatus.ready;status:moduleStatus;onManageRequested:panel.navigate("modules") }
            Label { textFormat:Text.PlainText; text:moduleStatus.ready && typeof panel.backend.moduleError==="function"?panel.backend.moduleError(section.modelData.owner):"";visible:text.length>0;color:theme.warning;font.pixelSize:theme.fontSmall;wrapMode:Text.WordWrap;Layout.fillWidth:true }
            Repeater {
                id:fields
                model:section.modelData.fields.filter(f=>!panel.inBar(section.modelData,f))
                delegate:UiField {
                    required property var modelData
                    objectName:"field_"+section.modelData.owner+"_"+(modelData.name || modelData.label)
                    Layout.fillWidth:true
                    prefix:modelData.prefix || "";suffix:modelData.suffix || "";hue:!!modelData.hue
                    lowLabel:modelData.lowLabel || "";highLabel:modelData.highLabel || ""
                    editContext:panel.page+"|"+panel.tdpScope+"|"+panel.app()
                    label:modelData.label; kind:modelData.kind; value:modelData.get()
                    note:typeof modelData.note==="function"?modelData.note():modelData.note || ""
                    choices:modelData.choices?modelData.choices():[]
                    minimum:typeof modelData.minimum==="function"?modelData.minimum():modelData.minimum || 0
                    maximum:typeof modelData.maximum==="function"?modelData.maximum():modelData.maximum || 100
                    visible:section.open && (modelData.visible?modelData.visible():true)
                    enabled:tick>=0 && panel.fieldEnabled(section.modelData.owner,modelData)
                    onEdited:function(value) { modelData.set(value); }
                    onTriggered:modelData.set()
                }
            }
            // Repair actions: one row at the bottom of the card, under a divider and a caption,
            // never next to the card's main action.
            Rectangle { visible:section.barShown && !!section.modelData.barTitle; Layout.fillWidth:true; implicitHeight:1; color:theme.divider; Layout.topMargin:4 }
            Label { textFormat:Text.PlainText; text:section.modelData.barTitle || ""; visible:text.length>0 && section.barShown; color:theme.muted; font.pixelSize:theme.fontSmall }
            Flow {
                visible:section.barShown
                Layout.fillWidth:true; spacing:10
                Repeater {
                    model:section.barFields
                    delegate:UiButton {
                        required property var modelData
                        objectName:"action_"+section.modelData.owner+"_"+modelData.label
                        text:String(modelData.get())
                        tone:modelData.danger?"danger":"secondary"
                        visible:section.open && (modelData.visible?modelData.visible():true)
                        enabled:tick>=0 && panel.fieldEnabled(section.modelData.owner,modelData)
                        onClicked:modelData.set()
                    }
                }
            }
        }
        }
        }
    }
    UiConfirm {
        id:dialog; objectName:"confirmDialog"; text:panel.confirmation
        onAccepted:panel.confirmed()
        onRejected:panel.pendingAction=null
    }
}
