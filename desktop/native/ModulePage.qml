// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
RowLayout {
    id:panel
    UiTheme { id:theme }
    required property var backend
    signal navigate(string page)
    property string page:"overview"
    property var cards:[]
    property string tdpScope:"global"
    property bool acSeparate:false
    property var powerDraft:({spl:15,sppt:20,fppt:25})
    property bool powerDirty:false
    property string powerNotice:""
    property string draftGame:""
    property string powerPreset:"custom"
    property string pendingMethod:""
    property var pendingArgs:[]
    property string confirmation:""
    property real tick:backend.now()
    property bool controllerDetails:false
    onTdpScopeChanged: { powerNotice=""; backend.cpuApp=powerTarget(); backend.cpuAc=tdpScope==="ac"; }
    readonly property bool compactPage:page==="controller" || page==="tdp"
    readonly property int columns:compactPage && width>=900?2:1
    spacing:18
    function s(method) { return backend ? backend.state(method) : {}; }
    function firmwareFresh() {
        const job=s("firmware_get_status")?.job;
        return tick>=0 && backend.connected && backend.age("firmware_get_status")<8 && !backend.readErrors?.firmware_get_status &&
            [job?.id,job?.message,job?.error].every(v=>v===undefined || typeof v==="string") &&
            ["idle","checking","downloading","validating","installing","verifying","restoring","succeeded","up_to_date","blocked","failed","failed_before_write","uncertain","stopped"].includes(job?.phase);
    }
    function firmwareIdle() { return firmwareFresh() && ["idle","succeeded","up_to_date","blocked","failed","failed_before_write","stopped"].includes(s("firmware_get_status").job?.phase); }
    function firmwareMessage() { return firmwareFresh() ? s("firmware_get_status").job?.error || s("firmware_get_status").job?.message || "Check for Lenovo updates for your controllers. Available updates are installed together." : backend.readErrors?.firmware_get_status || "Wait for a fresh firmware status before updating."; }
    function firmwarePhase() { return firmwareFresh() ? {checking:"Checking for updates",downloading:"Downloading update",validating:"Checking update",installing:"Updating controllers",verifying:"Verifying update",restoring:"Reconnecting controllers",succeeded:"Controllers updated",up_to_date:"No update available",blocked:"Update unavailable",failed:"Update failed",failed_before_write:"Update not started",uncertain:"Update needs attention",stopped:"Check stopped"}[s("firmware_get_status").job?.phase] || "Controller firmware" : "Status unavailable"; }
    function change(method,args) { backend.mutate(method,args || []); }
    function confirm(method,args,text) { pendingMethod=method; pendingArgs=args; confirmation=text; dialog.open(); }
    function pretty(value) { const labels={fps:"FPS",spg:"SPG",rpg:"RPG",max:"Maximum",ac:"Plugged in",game:"This game",balance_performance:"Balanced performance",balance_power:"Balanced power",power:"Power saving",default:"System default"};const text=String(value).replace(/_/g," ");return labels[value] || text.charAt(0).toUpperCase()+text.slice(1); }
    function powerOptions() { const presets=Object.keys(s("get_caps").presets || {}),order=["silent","balanced","performance","max"];return options(order.filter(v=>presets.indexOf(v)>=0).concat(presets.filter(v=>order.indexOf(v)<0 && v!=="custom"),["custom"])); }
    function options(values) { return (values || []).map(v=>typeof v==="object"?v:{id:v,label:pretty(v)}); }
    function indexedOptions(values) { return (values || []).map((label,id)=>({id:id,label:pretty(label)})); }
    function field(label,kind,get,set,extra) { return Object.assign({label:label,kind:kind,get:get,set:set,available:()=>true},extra || {}); }
    function text(label,get,extra) { return field(label,"text",get,()=>{},extra); }
    function notice(label,get) { return field(label,label?"text":"paragraph",get,()=>{},{visible:()=>String(get() || "").length>0}); }
    function action(label,method,args,extra) { return field(label,"button",()=>label,()=>change(method,args?args():[]),extra); }
    function switcher(label,get,method,args,extra) { return field(label,"toggle",get,v=>change(method,args?args(v):[v]),extra); }
    function choices(label,get,set,values,extra) { return field(label,"choice",get,set,Object.assign({choices:values},extra || {})); }
    function number(label,get,set,min,max,extra) { return field(label,"number",get,set,Object.assign({minimum:min,maximum:max},extra || {})); }
    function app() { return String(s("desktop_context").app_id || ""); }
    function powerTarget() { return tdpScope==="global"?"":app(); }
    function profileReady() { return tdpScope==="global" || (!!app() && backend.gameProfileApp===app() && backend.age("get_game_profile")<8); }
    function loadPower() {
        if(!backend || powerDirty || !profileReady()) return;
        let source=s("get_settings");
        if(tdpScope!=="global" && s("get_game_profile").exists)
            source=tdpScope==="ac"?s("get_game_profile").ac_profile || source:s("get_game_profile").profile || source;
        powerDraft={spl:Number(source.spl || 15000)/1000,sppt:Number(source.sppt || 20000)/1000,fppt:Number(source.fppt || 25000)/1000};
        powerPreset=source.preset || source.ac_preset || source.active_preset || "custom";
        acSeparate=!!s("get_game_profile").ac_separate;
        draftGame=app();
    }
    function powerLimit(key) { const caps=s("get_caps"); return Number((s("get_extras_unlocked")?caps.max:caps.std)?.[key] || 5); }
    function editPower(key,value) { powerNotice=""; const draft=Object.assign({},powerDraft); draft[key]=value; powerDraft=draft; powerDirty=true; powerPreset="custom"; }
    function applyPower() {
        if (!profileReady()) { backend.error="Wait for a fresh game profile before applying this draft."; return; }
        if(tdpScope!=="global" && app()!==draftGame) { backend.error="The running game changed. Reload this profile before applying the draft."; return; }
        if(tdpScope==="ac") change("set_game_ac_profile",[app(),Math.round(powerDraft.spl*1000),Math.round(powerDraft.sppt*1000),Math.round(powerDraft.fppt*1000),acSeparate,powerPreset]);
        else change("apply_tdp",[Math.round(powerDraft.spl*1000),Math.round(powerDraft.sppt*1000),Math.round(powerDraft.fppt*1000),powerTarget(),powerPreset,app()]);
    }
    function vibeArgs(value) { const state=s("vibe_get_settings"); return [value,state.app_id,state.profile_id]; }
    function rgbSettings() { return s("rgb_get_status").settings || {}; }
    function rgbUsesColor() { return ["monocolor","breathe"].indexOf(rgbSettings().effect)>=0; }
    function moduleFields(name) {
        const label=({tdp:"TDP",fan:"Fan Control",vibration:"Vibration",rgb:"Lighting",remap:"Button mapping",controller:"Gyro & touchpad",battery:"Battery",display:"Display",wifi:"Wi-Fi"})[name];
        const entry=()=>s("get_version").modules?.[name] || {};
        return [
            switcher(label,()=>entry().enabled,"modules_set_enabled",v=>[name,v],{
                available:()=>!entry().maintenance && !entry().pending,
                note:()=>entry().error || entry().note || (entry().pending?"Restoration is pending.":"")}),
            action("Retry "+label+" cleanup","modules_set_enabled",()=>[name,false],{
                visible:()=>!!entry().pending && !entry().maintenance}),
            action("Retry "+label+" start","modules_set_enabled",()=>[name,true],{
                visible:()=>entry().enabled===true && entry().ready===false && !entry().pending && !entry().maintenance})
        ];
    }
    function eppPercent() {
        const cpu=s("get_cpu_power_controls"),value=cpu.profile?.epp ?? cpu.epp?.value;
        const named={performance:0,balance_performance:128,balance_power:191,power:255};
        const raw=/^\d+$/.test(String(value))?Number(value):named[value];
        return raw===undefined?null:Math.round(raw*100/255);
    }
    function freshFan() { const state=s("fan_get_state"); return !!state.readings?.valid && Number(state.readings.age_seconds || 0)+backend.age("fan_get_state")<2.5; }
    function fanSpeed() { const state=s("fan_get_state"),rpm=state.readings?.rpm;
        return state.settings?.display_unit==="percent"?Math.round(Number(rpm)*100/Number(state.limits?.max_rpm || 4800))+"%":Math.round(Number(rpm))+" RPM"; }
    function fieldEnabled(owner,entry) {
        if(entry.kind==="text" || entry.kind==="paragraph")return true;
        if(owner==="rgb" && s("rgb_get_status").recovery_required)return false;
        const gate=s("get_version");
        return !!gate && typeof gate==="object" && !Array.isArray(gate) && gate.blocked===false &&
            backend.age("get_version")<8 && backend.connected && !backend.busy &&
            (page!=="tdp" || profileReady() || entry.label==="Editing profile") &&
            (owner==="about" || owner==="modules" || owner==="firmware" || !!gate.modules?.[owner]?.ready) && entry.available();
    }
    function forms() {
        const vibeReady=()=>!!s("vibe_is_ready").ready;
        const cpu=()=>s("get_cpu_power_controls");
        const cpuScopeReady=()=>String(cpu().profile?.app_id || "")===powerTarget() && !!cpu().profile?.ac_profile===(tdpScope==="ac");
        return {
            tdp:[{title:"Power",fields:[
                switcher("Enable TDP control",()=>s("get_settings").enabled,"set_plugin_enabled",null,{available:()=>!!s("get_caps").tdp_available}),
                choices("Editing profile",()=>tdpScope,v=>{ if(powerDirty) { backend.error="Apply or discard the TDP draft before changing profile."; return; } tdpScope=v; loadPower(); backend.issue("get_cpu_power_controls",[powerTarget(),tdpScope==="ac"]); },()=>options(app()?["global","game","ac"]:["global"]),{kind:"options"}),
                choices("Preset",()=>powerPreset,v=>{ const p=s("get_caps").presets?.[v]; if(p) { powerDraft={spl:Number(p.spl),sppt:Number(p.sppt),fppt:Number(p.fppt)}; powerDirty=true; } powerPreset=v; },powerOptions,{kind:"options"}),
                number("Sustained power",()=>powerDraft.spl,v=>editPower("spl",v),()=>Number(s("get_caps").min || 5),()=>powerLimit("spl"),{kind:"range",suffix:" W"}),
                number("Slow boost power",()=>powerDraft.sppt,v=>editPower("sppt",v),()=>Number(s("get_caps").min || 5),()=>powerLimit("sppt"),{kind:"range",suffix:" W",visible:()=>!!s("get_settings").advanced_tdp_control}),
                number("Fast boost power",()=>powerDraft.fppt,v=>editPower("fppt",v),()=>Number(s("get_caps").min || 5),()=>powerLimit("fppt"),{kind:"range",suffix:" W",visible:()=>!!s("get_settings").advanced_tdp_control}),
                field("Apply power limits","button",()=>"Apply limits",applyPower,{available:()=>profileReady() && !!s("get_caps").tdp_available && !!s("get_settings").enabled}),
                field("Discard draft","button",()=>"Discard draft",()=>{ powerDirty=false; loadPower(); }),
                field("Separate AC profile","toggle",()=>acSeparate,v=>{acSeparate=v;powerDirty=true;},{visible:()=>tdpScope==="ac"}),
                action("Delete game profile","delete_game_profile",()=>[app()],{visible:()=>tdpScope!=="global" && !!app()}),
                switcher("Advanced TDP limits",()=>s("get_settings").advanced_tdp_control,"set_advanced_tdp_control"),
                switcher("Extended power limits",()=>s("get_extras_unlocked"),"set_extras_unlocked",null,{visible:()=>!!s("get_caps").extras}),
                action("Retry helper setup","retry_extras",null,{visible:()=>!!s("get_caps").helper_retry_available}),
                action("Reapply saved settings","reapply"),
                text("Active limits (SPL / SPPT / FPPT)",()=>{
                    const values=s("get_tdp_info").values || {};
                    return ["spl_limit","sppt_limit","fppt_limit"].map(key=>values[key]===undefined || values[key]===null?"—":Number(values[key]).toFixed(0)).join(" / ")+" W";
                }),
                text("Measured APU power",()=>{
                    const value=s("get_tdp_info").values?.package_draw;
                    return value===undefined || value===null?"—":Number(value).toFixed(1)+" W";
                }),
                notice("Power control",()=>s("get_caps").tdp_error || ""),
                notice("Power profile",()=>powerNotice)
            ]},{title:"CPU power controls",fields:[
                switcher("CPU boost",()=>cpu().profile?.cpu_boost_enabled ?? cpu().cpu_boost?.enabled,"set_cpu_boost",v=>[v,powerTarget(),tdpScope==="ac",app()],{available:()=>cpuScopeReady() && !!cpu().cpu_boost?.available}),
                number("CPU energy preference",eppPercent,v=>change("set_epp",[String(Math.round(v*255/100)),powerTarget(),tdpScope==="ac",app()]),0,100,{kind:"range",suffix:"%",lowLabel:"Performance",highLabel:"Energy saving",visible:()=>!!cpu().epp?.numeric_supported && eppPercent()!==null,available:()=>cpuScopeReady() && !!cpu().epp?.available}),
                choices("EPP profiles",()=>cpu().profile?.epp ?? cpu().epp?.value,v=>change("set_epp",[v,powerTarget(),tdpScope==="ac",app()]),()=>options(cpu().epp?.profiles || []),{kind:"options",available:()=>cpuScopeReady() && !!cpu().epp?.available}),
                notice("CPU control",()=>cpu().error || cpu().epp?.compatibility_note || "")
            ]}],
            vibration:[{title:"Vibration & haptics",fields:[
                choices("Vibration level",()=>s("vibe_get_settings").settings?.level,v=>change("vibe_set_intensity",vibeArgs(v)),()=>indexedOptions(s("vibe_get_capabilities").intensity),{available:vibeReady}),
                choices("Rumble mode",()=>s("vibe_get_settings").settings?.mode,v=>change("vibe_set_rumble_mode",vibeArgs(v)),()=>indexedOptions(s("vibe_get_capabilities").mode),{available:vibeReady}),
                switcher("Touchpad haptics",()=>s("vibe_get_settings").settings?.touchpadEnabled,"vibe_set_touchpad_enabled",vibeArgs,{available:vibeReady}),
                choices("Touchpad intensity",()=>s("vibe_get_settings").settings?.touchpadIntensity,v=>change("vibe_set_touchpad_intensity",vibeArgs(v)),()=>indexedOptions(s("vibe_get_capabilities").tp_intensity),{available:vibeReady}),
                switcher("Separate settings for this game",()=>s("vibe_get_settings").overwrite,"vibe_set_profile_overwrite",v=>[app(),v,""],{visible:()=>!!app(),available:vibeReady}),
                action("Test vibration","vibe_test_vibration",()=>[500],{available:vibeReady}),
                action("Reapply settings","vibe_reapply",null,{available:vibeReady}),
                action("Reset haptics","vibe_reset_to_default",()=>[s("vibe_get_settings").app_id,s("vibe_get_settings").profile_id],{available:vibeReady}),
                notice("Vibration",()=>s("vibe_is_ready").error || "")
            ]}],
            rgb:[{title:"Controller lighting",fields:[
                switcher("Control lighting",()=>rgbSettings().control_enabled,"rgb_set_control_enabled",null,{available:()=>!!s("rgb_get_status").rgb?.supported}),
                switcher("Joystick rings",()=>rgbSettings().rings_enabled,"rgb_set_rings_enabled",null,{available:()=>!!rgbSettings().control_enabled}),
                choices("Effect",()=>rgbSettings().effect,v=>change("rgb_set_effect",[v]),()=>s("rgb_get_status").rgb?.effects || [],{kind:"options",available:()=>!!rgbSettings().control_enabled}),
                number("Hue",()=>rgbSettings().hue,v=>change("rgb_set_color",[v,Number(rgbSettings().saturation || 0)]),0,359,{kind:"range",hue:true,suffix:"°",visible:rgbUsesColor,available:()=>!!rgbSettings().control_enabled}),
                number("Saturation",()=>rgbSettings().saturation,v=>change("rgb_set_color",[Number(rgbSettings().hue || 0),v]),0,100,{kind:"range",suffix:"%",visible:rgbUsesColor,available:()=>!!rgbSettings().control_enabled}),
                number("Brightness",()=>rgbSettings().brightness,v=>change("rgb_set_brightness",[v]),0,100,{kind:"range",suffix:"%",available:()=>!!rgbSettings().control_enabled}),
                number("Animation speed",()=>rgbSettings().speed,v=>change("rgb_set_speed",[v]),0,100,{kind:"range",suffix:"%",visible:()=>rgbSettings().effect!=="monocolor",available:()=>!!rgbSettings().control_enabled}),
                switcher("Power LED",()=>rgbSettings().power_led_enabled,"rgb_set_power_led",null,{available:()=>!!s("rgb_get_status").power_led?.supported}),
                action("Restore original lighting","rgb_restore_original"),
                notice("Lighting",()=>s("rgb_get_status").error || s("rgb_get_status").power_led?.reason || "")
            ]}],
            remap:[{title:"Button mapping",fields:[
                switcher("Enable button mapping",()=>s("remap_get_status").enabled,"remap_set_enabled",null,{available:()=>!!s("remap_get_status").supported}),
                choices("Legion button",()=>s("remap_get_status").desktop_action,v=>change("remap_set_action",["desktop",v]),()=>s("remap_get_status").actions || []),
                choices("Quick settings button",()=>s("remap_get_status").page_action,v=>change("remap_set_action",["page",v]),()=>s("remap_get_status").actions || []),
                action("Restore default mapping","remap_restore_defaults"),
                notice("Button mapping",()=>s("remap_get_status").error || (!s("remap_get_status").supported?s("remap_get_status").reason || "":""))
            ]}],
            controller:[{title:"Controller firmware",owner:"firmware",fields:[
                notice("",firmwareMessage),
                action("Check updates","firmware_check_updates",null,{available:()=>firmwareIdle()}),
                text("Status",()=>firmwarePhase())
            ]},{title:"Gyroscope & touchpad",fields:[
                choices("Gyro source",()=>s("controller_get_status").gyro_source,v=>change("controller_set_gyro_source",[v]),()=>options(["system","combined","left","right"]),{available:()=>!!s("controller_get_status").available && !s("controller_get_status").conflict}),
                action("Release controller override","controller_release_control"),
                action("Start diagnostics","controller_start_diagnostics",null,{visible:()=>controllerDetails,available:()=>!backend.diagnosticToken && !!s("controller_get_status").available}),
                action("Stop diagnostics","controller_stop_diagnostics",()=>[backend.diagnosticToken],{visible:()=>controllerDetails,available:()=>!!backend.diagnosticToken}),
                text("Physical controller",()=>s("controller_get_status").physical?.path || "—",{visible:()=>controllerDetails}),
                text("Virtual controller",()=>s("controller_get_status").virtual?.path || "—",{visible:()=>controllerDetails}),
                text("IMU",()=>JSON.stringify(s("controller_get_status").imu?.actual || {}),{visible:()=>controllerDetails}),
                text("Diagnostic stream",()=>JSON.stringify(s("controller_get_diagnostics").virtual || {}),{visible:()=>controllerDetails && !!backend.diagnosticToken}),
                notice("Controller",()=>s("controller_get_status").error || (!s("controller_get_status").available || s("controller_get_status").conflict?s("controller_get_status").reason || "":""))
            ]}],
            battery:[{title:"Charge management",fields:[
                switcher("Battery protection",()=>s("battery_get_status").enabled,"battery_set_enabled",null,{available:()=>!!s("battery_get_status").supported}),
                action("Release battery control","battery_release_control"),
                text("Charge",()=>s("battery_get_status").capacity===undefined?"—":s("battery_get_status").capacity+"%"),
                text("Charging status",()=>s("battery_get_status").charging_status || "—"),
                text("Firmware mode",()=>s("battery_get_status").current_mode || "—"),
                text("Left controller",()=>s("battery_get_controller_levels").left===undefined || s("battery_get_controller_levels").left===null?"—":s("battery_get_controller_levels").left+"%"),
                text("Right controller",()=>s("battery_get_controller_levels").right===undefined || s("battery_get_controller_levels").right===null?"—":s("battery_get_controller_levels").right+"%"),
                notice("Battery control",()=>s("battery_get_status").error || s("battery_get_status").reason || "")
            ]}],
            display:[
                {title:"Display",visible:()=>nativeDisplay(),badge:()=>confirmedNativeDisplay()?"Upstream display support":"Display support",badgeTone:()=>confirmedNativeDisplay()?"green":"amber",note:"",fields:[
                    notice("",()=>s("display_get_state").native_support_reason || "Native display support was detected earlier. Waiting for confirmation."),
                    notice("Cleanup",()=>s("display_get_state").native_cleanup_error || (s("display_get_state").native_cleanup_pending?"Previous display adjustments are being removed.":"")),
                    field("Apply pending session changes","button",()=>"Restart Gaming Mode session",()=>confirm("display_restart_session",[],"Restart the Gaming Mode session to finish removing previous display adjustments?"),{visible:()=>!!s("display_get_state").native_restart_pending,available:()=>!!s("desktop_context").gamescope})
                ]},
                {title:"Display Fix",visible:()=>!nativeDisplay(),note:"Changes that require a session restart can be applied in Gaming Mode.",fields:[
                    switcher("Enable display correction",()=>s("display_get_state").enabled,"display_set_enabled",null,{available:()=>!!s("display_get_state").setup_done}),
                    choices("Panel mode",()=>s("display_get_state").panel_mode,v=>change("display_set_panel_mode",[v]),()=>[{id:"gamma22",label:"Gamma 2.2"},{id:"pq",label:"PQ"},{id:"hybrid",label:"Hybrid"}]),
                    field("Run display setup","button",()=>"Set up",()=>confirm("display_run_setup",[s("display_get_state").panel_mode || "pq"],"Set up the selected display correction?")),
                    field("Reset display settings","button",()=>"Reset",()=>confirm("display_reset_settings",[],"Restore the display module's original settings?")),
                    field("Apply pending session changes","button",()=>"Restart Gaming Mode session",()=>confirm("display_restart_session",[],"Restart the Gaming Mode session to apply pending display changes?"),{available:()=>!!s("desktop_context").gamescope}),
                    text("Active mode",()=>s("display_get_state").active_mode || "Inactive"),
                    text("Panel",()=>s("display_get_state").panel_desc || "—"),
                    text("Brightness",()=>s("display_get_state").nits===undefined?"—":s("display_get_state").nits+" nits"),
                    notice("Display support",()=>s("display_get_state").reason || s("display_get_state").setup_error || "")
                ]},
                {title:"EDID for games",note:"Corrects the OLED brightness information reported to games. Separate from the system's display support.",fields:[
                    switcher("EDID brightness fix",()=>s("display_get_state").edid_fix,"display_set_edid_fix"),
                    text("Status",()=>s("display_get_state").edid_patched?"Applied":"Standby"),
                    notice("",()=>s("display_get_state").edid_reason || ""),
                    text("Reported peak brightness",()=>Number(s("display_get_state").edid_game_nits).toFixed(0)+" nits",{visible:()=>!!s("display_get_state").edid_patched && Number(s("display_get_state").edid_game_nits)>0})
                ]}
            ],
            wifi:[{title:"Wi-Fi bands",fields:[
                choices("Band policy",()=>s("wifi_get_status").settings?.band_policy,v=>confirm("wifi_set_band_policy",[v,false],"Apply this Wi-Fi band policy? The connection may be interrupted."),()=>[{id:"off",label:"All supported bands"},{id:"five_six_no_24",label:"5/6 GHz without 2.4 GHz"},{id:"five_six_only",label:"Require 5/6 GHz"},{id:"six_ghz_only",label:"Require 6 GHz"}]),
                field("Rescan and reconnect (5/6 GHz)","button",()=>"Reconnect",()=>confirm("wifi_rescan_and_reconnect",["high"],"Rescan and reconnect? This may interrupt the current network session.")),
                field("Rescan and reconnect (6 GHz)","button",()=>"Reconnect",()=>confirm("wifi_rescan_and_reconnect",["six"],"Try a 6 GHz connection? This may interrupt the current connection.")),
                text("Network",()=>s("wifi_get_status").live?.ssid || "Disconnected"),
                text("Frequency",()=>s("wifi_get_status").live?.frequency || "—"),
                text("Driver / backend",()=>[s("wifi_get_status").settings?.driver,s("wifi_get_status").live?.wifi_backend].filter(Boolean).join(" / ")),
                notice("Connection",()=>s("wifi_get_status").error || s("wifi_get_status").live?.policy_error || s("wifi_get_status").message || "")
            ]}],
            modules:[{title:"Choose active modules",note:"",fields:["tdp","fan","vibration","rgb","remap","controller","battery","display","wifi"].reduce((fields,name)=>fields.concat(moduleFields(name)),[])}],
            about:[{title:"Legion Go Companion",fields:[
                text("Installed version",()=>s("get_version").version || "—"),
                action("Check for updates","updates_check"),
                action("Download release ZIP","updates_download",()=>[s("updates_check").latest_version],{available:()=>!!s("updates_check").update_available && !!s("updates_check").download_available}),
                text("Latest public release",()=>s("updates_check").latest_version || "Not checked"),
                text("Downloaded ZIP",()=>s("updates_download").path || "—")
            ]}]
        };
    }
    function nativeDisplay() { const state=s("display_get_state");return state.native_support_detected===true || state.native_cleanup_pending===true; }
    function confirmedNativeDisplay() { const state=s("display_get_state");return state.native_support_detected===true && String(state.native_support_reason || "").indexOf("Gamescope includes the native Legion Go 2 display profile.")===0; }
    function rebuild() {
        const sections=forms();
        const owners=page==="controller"?["vibration","rgb","remap","controller"]:[page];
        let merged=[];
        for(const owner of owners) merged=merged.concat((sections[owner] || []).map(card=>Object.assign({owner:owner},card)));
        cards=merged;
    }
    onPageChanged: { tdpScope="global"; powerDirty=false; controllerDetails=false; loadPower(); rebuild(); }
    Component.onCompleted: { loadPower(); rebuild(); }
    Timer { interval:500; repeat:true; running:backend.windowActive; onTriggered:panel.tick=backend.now() }
    Connections {
        target:panel.backend
        function onChanged(method) {
            if(method==="get_settings" || method==="get_game_profile") panel.loadPower();
            if(method==="desktop_context" && panel.page==="tdp") { backend.cpuApp=panel.powerTarget(); backend.cpuAc=panel.tdpScope==="ac"; }
        }
        function onMutationDone(method,result,success) {
            if(method==="apply_tdp" || method==="set_game_ac_profile") {
                if(success)panel.powerDirty=false;
                panel.powerNotice=success && result?.saved_only ? result.message || "Global TDP settings saved. They will apply when no game profile is active." : "";
            }
        }
    }
    Repeater {
        model:panel.columns
        delegate:ColumnLayout {
        id:cardColumn
        required property int index
        Layout.fillWidth:true; Layout.minimumWidth:0; Layout.alignment:Qt.AlignTop
        spacing:18
        Repeater {
        model:panel.cards.filter((card,i)=>i%panel.columns===cardColumn.index)
        delegate:UiCard {
            id:section
            required property var modelData
            objectName:"moduleSection_"+modelData.owner
            visible:!modelData.visible || modelData.visible()
            readonly property bool gated:["about","modules","firmware"].indexOf(modelData.owner)<0
            ModuleStatus { id:moduleStatus;backend:panel.backend;owner:section.modelData.owner;tick:panel.tick }
            Layout.fillWidth:true; Layout.alignment:Qt.AlignTop; Layout.minimumWidth:0
            padding:20
            spacing:panel.compactPage?10:14
            function preview(label,fallback) { for(let i=0;i<fields.count;i++){const item=fields.itemAt(i);if(item && item.label===label)return item.liveValue;}return fallback; }
            RowLayout { Layout.fillWidth:true; spacing:10
                UiIcon { name:section.modelData.owner; color:theme.accentText; Layout.preferredWidth:20; Layout.preferredHeight:20 }
                Label { textFormat:Text.PlainText; text:section.modelData.title; font.pixelSize:17; font.weight:Font.DemiBold; color:theme.text; Layout.fillWidth:true; wrapMode:Text.WordWrap }
                UiBadge { visible:moduleStatus.ready && !!section.modelData.badge;text:section.modelData.badge?section.modelData.badge():"";tone:section.modelData.badgeTone?section.modelData.badgeTone():"blue" }
                UiBadge { visible:section.gated && !moduleStatus.ready;text:moduleStatus.label;tone:["disabled","offline","loading"].includes(moduleStatus.state)?"neutral":"amber" }
            }
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
            Label { textFormat:Text.PlainText; text:modelData.note || ""; visible:text.length>0 && (!section.gated || moduleStatus.ready); wrapMode:Text.WordWrap; color:theme.muted; font.pixelSize:13; Layout.fillWidth:true }
            UiModuleNotice { Layout.fillWidth:true;visible:section.gated && !moduleStatus.ready;status:moduleStatus;onManageRequested:panel.navigate("modules") }
            Label { textFormat:Text.PlainText; text:moduleStatus.ready && typeof panel.backend.moduleError==="function"?panel.backend.moduleError(section.modelData.owner):"";visible:text.length>0;color:theme.warning;font.pixelSize:12;wrapMode:Text.WordWrap;Layout.fillWidth:true }
            Rectangle { visible:!section.gated || moduleStatus.ready;Layout.fillWidth:true; implicitHeight:1; color:theme.divider }
            RowLayout {
                visible:moduleStatus.ready && section.modelData.owner==="rgb" && panel.rgbUsesColor();Layout.fillWidth:true
                Label { textFormat:Text.PlainText; text:"Color preview";color:theme.muted;font.pixelSize:12;Layout.fillWidth:true }
                Repeater { model:2;Rectangle { width:26;height:26;radius:13;color:"transparent";border.width:4;border.color:panel.rgbUsesColor()?Qt.hsva(section.preview("Hue",0)/360,section.preview("Saturation",100)/100,section.preview("Brightness",100)/100,1):theme.accentText } }
            }
            Repeater {
                id:fields
                model:panel.compactPage?section.modelData.fields.filter(f=>f.kind!=="button"):section.modelData.fields
                delegate:UiField {
                    required property var modelData
                    objectName:"field_"+section.modelData.owner+"_"+modelData.label
                    Layout.fillWidth:true
                    compact:panel.compactPage
                    suffix:modelData.suffix || "";hue:!!modelData.hue
                    lowLabel:modelData.lowLabel || "";highLabel:modelData.highLabel || ""
                    editContext:panel.page+"|"+panel.tdpScope+"|"+panel.app()
                    label:modelData.label; kind:modelData.kind; value:modelData.get()
                    note:typeof modelData.note==="function"?modelData.note():modelData.note || ""
                    choices:modelData.choices?modelData.choices():[]
                    minimum:typeof modelData.minimum==="function"?modelData.minimum():modelData.minimum || 0
                    maximum:typeof modelData.maximum==="function"?modelData.maximum():modelData.maximum || 100
                    visible:(!section.gated || moduleStatus.ready) && (modelData.visible?modelData.visible():true)
                    enabled:tick>=0 && panel.fieldEnabled(section.modelData.owner,modelData)
                    onEdited:function(value) { modelData.set(value); }
                    onTriggered:modelData.set()
                }
            }
            Flow {
                visible:panel.compactPage && (!section.gated || moduleStatus.ready)
                Layout.fillWidth:true; spacing:8
                Repeater {
                    model:section.modelData.fields.filter(f=>f.kind==="button")
                    delegate:UiButton {
                        required property var modelData
                        objectName:"action_"+section.modelData.owner+"_"+modelData.label
                        text:String(modelData.get()); font.pixelSize:12; implicitHeight:35
                        tone:modelData.label==="Apply power limits"?"primary":"secondary"
                        visible:(!section.gated || moduleStatus.ready) && (modelData.visible?modelData.visible():true)
                        enabled:tick>=0 && panel.fieldEnabled(section.modelData.owner,modelData)
                        onClicked:modelData.set()
                    }
                }
                UiButton {
                    visible:section.modelData.owner==="controller"
                    text:panel.controllerDetails?"Hide details":"Device details"; tone:"quiet"; font.pixelSize:12; implicitHeight:35
                    enabled:!backend.busy
                    onClicked: {
                        if(panel.controllerDetails && backend.diagnosticToken)backend.mutate("controller_stop_diagnostics",[backend.diagnosticToken]);
                        panel.controllerDetails=!panel.controllerDetails;
                    }
                }
            }
        }
        }
        }
    }
    Dialog {
        id:dialog; title:"Confirm change"; modal:true; standardButtons:Dialog.Ok|Dialog.Cancel
        Label { textFormat:Text.PlainText; text:panel.confirmation; color:"#e4ebf2"; width:440; wrapMode:Text.WordWrap }
        onAccepted:panel.change(panel.pendingMethod,panel.pendingArgs)
    }
}
