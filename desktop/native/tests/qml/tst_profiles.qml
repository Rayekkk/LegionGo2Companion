// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtTest
import "../.."
TestCase {
    name:"NativeProfileSafety"; when:windowShown; width:1000;height:1000
    visible:running; z:1000
    QtObject {
        id:fake
        property var data:({})
        property var calls:[]
        property var reads:[]
        property bool connected:true
        property bool busy:false
        property bool windowActive:false
        property string error:""
        property string diagnosticToken:""
        property string cpuApp:""
        property bool cpuAc:false
        property string gameProfileApp:"42"
        property var readErrors:({})
        property real firmwareAge:0
        signal changed(string method)
        signal mutationDone(string method,var result,bool success)
        function state(method) { return data[method]===undefined?{}:data[method]; }
        function now() { return 0; }
        function age(method) { return method==="firmware_get_status"?firmwareAge:0; }
        function issue(method,args) { reads=reads.concat([{method:method,args:args}]); }
        function mutate(method,args,then) { calls=calls.concat([{method:method,args:args,then:then || null}]); }
    }
    Component { id:factory; ModulePage { width:950;backend:fake;page:"tdp" } }
    property var panel
    function init() {
        fake.calls=[];fake.reads=[];fake.error="";fake.connected=true;fake.gameProfileApp="42";fake.busy=false;fake.readErrors={};fake.firmwareAge=0;
        fake.data={get_version:{blocked:false,modules:{}},desktop_context:{app_id:"42"},get_settings:{spl:15000,sppt:20000,fppt:25000,enabled:true},
            get_caps:{min:5,std:{spl:30,sppt:35,fppt:40},tdp_available:true,presets:{}},get_game_profile:{exists:false}};
        panel=createTemporaryObject(factory,this);verify(panel!==null);
    }
    function allFields(owner,cardOwner) {
        let fields=[];for(const card of panel.forms()[owner])if(!cardOwner || card.owner===cardOwner)fields=fields.concat(card.fields);return fields;
    }
    function field(owner,label) {
        const entry=allFields(owner).find(item=>item.label===label);
        verify(entry!==undefined,"Field: "+owner+" / "+label);return entry;
    }
    function card(title) {
        const entry=panel.forms().display.find(item=>item.title===title);
        verify(entry!==undefined,"Display card: "+title);return entry;
    }
    function test_globalUsesSharedMillwattRpcAndCurrentGameGuard() {
        panel.editPower("spl",18);panel.applyPower();
        compare(fake.calls[0].method,"apply_tdp");compare(fake.calls[0].args[0],18000);
        compare(fake.calls[0].args[3],"");compare(fake.calls[0].args[5],"42");
    }
    // As in Decky: placeholders never show as limits, About names the check result, and a missing driver is said.
    function test_panelRulesSharedWithDecky() {
        fake.data=Object.assign({},fake.data,{get_settings:{enabled:true}});
        panel=createTemporaryObject(factory,this);
        verify(allFields("tdp").some(item=>item.kind==="paragraph" && item.get()==="Loading power limits…"));
        compare(field("tdp","Preset").visible(),false);
        compare(field("tdp","Sustained power").visible(),false);
        fake.data=Object.assign({},fake.data,{get_settings:{spl:15000,sppt:20000,fppt:25000,enabled:true}});
        panel.loadPower();
        compare(field("tdp","Preset").visible(),true);
        verify(!allFields("tdp").some(item=>item.kind==="paragraph" && item.get()==="Loading power limits…"));
        fake.data=Object.assign({},fake.data,{get_version:{version:"2.2.1",modules:{}},updates_check:{success:true,current_version:"2.2.1",no_release:true,update_available:false,download_available:false}});
        compare(field("about","Updates").get(),"No public release yet");
        fake.data=Object.assign({},fake.data,{updates_check:{}});
        compare(field("about","Updates").get(),"GitHub releases");
        fake.data=Object.assign({},fake.data,{vibe_get_driver_status:{found:false}});
        verify(allFields("vibration").some(item=>item.get()==="Controller driver not found. Needs SteamOS 3.8 or newer."));
        fake.data=Object.assign({},fake.data,{vibe_get_driver_status:{found:true}});
        verify(!allFields("vibration").some(item=>item.get()==="Controller driver not found. Needs SteamOS 3.8 or newer."));
    }
    function test_modulesExposeTheExistingCleanupAndStartupRetryActions() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{
            fan:{enabled:false,ready:false,pending:true,error:"Restoration is pending."},
            rgb:{enabled:true,ready:false,pending:false,error:"Controller unavailable."}
        }}});
        panel.page="modules";
        const cleanup=findChild(panel,"field_modules_Retry Fan Control cleanup");
        verify(cleanup!==null,"A pending disabled module needs a reachable cleanup retry");
        tryVerify(function(){return cleanup.visible && cleanup.width>0;});
        const toggle=findChild(panel,"field_modules_Fan Control");verify(toggle!==null);verify(!toggle.enabled);
        mouseClick(cleanup,30,cleanup.height/2); // a self-describing button sits at the left
        compare(fake.calls[0].method,"modules_set_enabled");compare(fake.calls[0].args[0],"fan");compare(fake.calls[0].args[1],false);
        const startup=findChild(panel,"field_modules_Retry Lighting start");verify(startup!==null);verify(startup.visible);
        mouseClick(startup,30,startup.height/2);
        compare(fake.calls[1].args[0],"rgb");compare(fake.calls[1].args[1],true);
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{
            fan:{enabled:false,ready:true,pending:false},rgb:{enabled:true,ready:true,pending:false}
        }}});
        verify(!cleanup.visible);verify(!startup.visible);verify(toggle.enabled);
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{
            fan:{enabled:false,ready:false,pending:true,maintenance:true},
            rgb:{enabled:true,ready:false,pending:false,maintenance:true}
        }}});
        verify(!cleanup.visible);verify(!startup.visible);verify(!toggle.enabled);
    }
    function test_moduleSwitchShowsTheSavedChoiceAndTheSharedNote() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{rgb:{enabled:true,ready:false,error:"Startup failed"},battery:{enabled:false}}}});
        const lighting=field("modules","Lighting");
        verify(lighting.get(),"a module that failed to start reads On, so it can be turned off");compare(lighting.note(),"Startup failed");
        verify(lighting.available());lighting.set(false);compare(fake.calls[0].args.join(),"rgb,false");
        compare(field("modules","Battery").note(),"Charge limit of about 80%.");
    }
    function test_moduleSwitchTurnsOffASlowStart() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{rgb:{enabled:true,ready:false,starting:true,error:""}}}});
        const lighting=field("modules","Lighting");
        verify(lighting.get());verify(lighting.available());compare(lighting.note(),"Still starting. Its controls appear when it has finished.");
        verify(!field("modules","Retry Lighting start").visible());
        lighting.set(false);compare(fake.calls[0].args.join(),"rgb,false");
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{rgb:{enabled:false,ready:false,starting:true,error:""}}}});
        fake.mutationDone("modules_set_enabled",fake.data.get_version.modules,true);
        verify(!lighting.get());verify(lighting.available());compare(lighting.note(),"Turns off when it has finished starting.");
    }
    function test_inactiveGlobalSaveExplainsThatTheGameProfileStillApplies() {
        panel.editPower("spl",18);
        fake.mutationDone("apply_tdp",{success:true,saved_only:true,applied:false,
            message:"Global TDP settings saved. They will apply when no game profile is active."},true);
        compare(panel.powerDirty,false);
        compare(field("tdp","Power profile").get(),"Saved for Global. It applies when no game profile is active.");
        panel.editPower("spl",19);compare(field("tdp","Power profile").get(),"");
        fake.mutationDone("apply_tdp",{success:false},false);
        compare(panel.powerDirty,true);compare(field("tdp","Power profile").get(),"");
        fake.mutationDone("apply_tdp",{success:true,applied:true},true);
        compare(panel.powerDirty,false);compare(field("tdp","Power profile").get(),"");
    }
    function test_changedGameDoesNotReceiveOldDraft() {
        panel.tdpScope="game";panel.loadPower();panel.editPower("spl",18);
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"99"}});
        panel.applyPower();compare(fake.calls.length,0);verify(fake.error.length>0);
    }
    function test_separateAcProfileUsesExistingBackendSchema() {
        panel.tdpScope="ac";panel.loadPower();panel.acSeparate=true;panel.editPower("spl",17);panel.applyPower();
        compare(fake.calls[0].method,"set_game_ac_profile");compare(fake.calls[0].args[0],"42");
        compare(fake.calls[0].args[1],17000);compare(fake.calls[0].args[4],true);
    }
    function test_vibrationUsesPublishedCapabilityKeysAndNumericIds() {
        fake.data=Object.assign({},fake.data,{
            vibe_get_settings:{settings:{level:2,mode:2,touchpadIntensity:2,touchpadEnabled:true},app_id:"0",profile_id:"0"},
            vibe_get_capabilities:{intensity:["off","low","medium","high"],mode:["fps","racing","standard","spg","rpg"],tp_intensity:["off","low","medium","high"]}
        });
        panel.page="vibration";
        const level=field("vibration","Vibration strength"),mode=field("vibration","Vibration mode"),touchpad=field("vibration","Touchpad strength");
        compare(level.choices().length,4);compare(level.choices()[2].label,"Medium");
        compare(mode.choices().length,5);compare(mode.choices()[2].id,2);
        compare(touchpad.choices().length,4);
        level.set(3);compare(fake.calls[0].method,"vibe_set_intensity");
        compare(fake.calls[0].args[0],3);compare(fake.calls[0].args[1],"0");
    }
    function test_presetAppliesImmediatelyWithAbsoluteBackendWattValues() {
        fake.data=Object.assign({},fake.data,{get_caps:{min:5,std:{spl:30,sppt:35,fppt:40},tdp_available:true,presets:{balanced:{spl:16,sppt:25,fppt:30}}}});
        panel.editPower("spl",21);verify(panel.powerDirty);
        field("tdp","Preset").set("balanced");
        compare(fake.calls.length,1,"A preset applies at once, with no Apply step");
        compare(fake.calls[0].method,"apply_tdp");compare(fake.calls[0].args[0],16000);
        compare(fake.calls[0].args[1],25000);compare(fake.calls[0].args[2],30000);
        compare(fake.calls[0].args[4],"balanced");
        verify(!panel.powerDirty,"Picking a preset drops the Custom draft without asking");
        verify(!field("tdp","Sustained power").visible(),"The sliders exist only in Custom");
    }
    function test_customOnlyOpensTheSlidersAndApplyCommitsTheDraft() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{tdp:{enabled:true,ready:true}}}});
        const preset=field("tdp","Preset");
        panel.powerPreset="balanced";verify(!field("tdp","Sustained power").visible());
        preset.set("custom");compare(fake.calls.length,0,"Custom applies nothing by itself");
        verify(field("tdp","Sustained power").visible());
        panel.loadPower();compare(panel.powerPreset,"custom","Polling cannot close the Custom sliders");
        const row=findChild(panel,"field_tdp_Custom limits");verify(row!==null);tryCompare(row,"visible",true);
        const apply=findChild(row,"button_apply");verify(apply!==null);verify(!apply.enabled);
        panel.editPower("spl",18);compare(row.value,"Unsaved changes");
        const ready=findChild(row,"button_apply");verify(ready.enabled);compare(ready.text,"Apply");
        ready.clicked();compare(fake.calls[0].method,"apply_tdp");compare(fake.calls[0].args[0],18000);compare(fake.calls[0].args[4],"custom");
    }
    function gameProfile(extra) {
        return Object.assign({exists:true,active:true,ac_separate:false,profile:{spl:20000,sppt:25000,fppt:30000,preset:"custom"},
            ac_profile:{spl:22000,sppt:27000,fppt:32000,ac_preset:""}},extra || {});
    }
    function test_scopeSelectorKeepsTheGameProfileWhenSwitchingToGlobal() {
        fake.data=Object.assign({},fake.data,{get_game_profile:gameProfile()});
        const scope=field("tdp","Settings for this game");verify(scope.visible());verify(scope.available());
        compare(scope.get(),"game");
        scope.set("global");
        compare(fake.calls.length,1);compare(fake.calls[0].method,"set_game_profile_active");
        compare(fake.calls[0].args[0],"42");compare(fake.calls[0].args[1],false);
        verify(!fake.calls.some(c=>c.method==="delete_game_profile"),"Turning the profile off never deletes it");
        fake.data=Object.assign({},fake.data,{get_game_profile:gameProfile({active:false})});
        compare(scope.get(),"global");
    }
    function test_thisGameCreatesTheProfileAtOnceLikeDecky() {
        field("tdp","Settings for this game").set("game");
        compare(fake.calls.length,1,"No Apply or preset needed");
        compare(fake.calls[0].method,"set_game_profile_active");compare(fake.calls[0].args.join(","),"42,true");
        compare(fake.calls[0].then,null);compare(panel.tdpScope,"game");
        fake.calls=[];fake.data=Object.assign({},fake.data,{get_caps:Object.assign({},fake.data.get_caps,{tdp_available:false})});
        panel.tdpScope="global";field("tdp","Settings for this game").set("game");
        compare(fake.calls.length,0,"Without a TDP provider the choice only picks the CPU settings to edit");
    }
    function test_scopeSelectorReactivatesAndSplitsTheChargerProfile() {
        fake.data=Object.assign({},fake.data,{get_game_profile:gameProfile({active:false})});
        const scope=field("tdp","Settings for this game");
        scope.set("charger");
        compare(fake.calls[0].method,"set_game_profile_active");compare(fake.calls[0].args[1],true);
        // Backend sends the split after the reactivation succeeds (tst_shared); its arguments are fixed at the click.
        compare(fake.calls[0].then.method,"set_game_ac_profile");
        compare(fake.calls[0].then.args.join(","),"42,22000,27000,32000,true,,false","The stored charger values are kept");
        compare(fake.calls.length,1);
        fake.calls=[];fake.data=Object.assign({},fake.data,{get_game_profile:gameProfile({ac_separate:true})});
        compare(scope.get(),"charger");verify(field("tdp","Edit settings for").visible());
        scope.set("game");
        compare(fake.calls[0].method,"set_game_ac_profile");compare(fake.calls[0].args[4],false);
    }
    function test_changingScopeWithAnUnappliedDraftAsksFirst() {
        fake.data=Object.assign({},fake.data,{get_game_profile:gameProfile()});
        panel.tdpScope="game";panel.loadPower();panel.editPower("spl",18);
        field("tdp","Settings for this game").set("global");
        compare(fake.calls.length,0,"No RPC before the draft is discarded");
        const dialog=confirmDialog();compare(dialog.title,"Discard unsaved changes?");compare(dialog.okText,"Discard");
        compare(panel.confirmation,"Your changes to these limits haven't been applied.");
        dialog.accept();
        verify(!panel.powerDirty);compare(fake.calls[0].method,"set_game_profile_active");compare(panel.tdpScope,"global");
    }
    function switchIn(item) {
        if(item.checked!==undefined && item.indicator!==undefined)return item;
        for(const child of item.children || []) { const found=switchIn(child);if(found)return found; }
        return null;
    }
    function test_unmanagedPowerLightShowsTheLightAndTheFirstClickTurnsItOn() {
        const status=managed=>({settings:{control_enabled:true,power_led_enabled:true,power_led_managed:managed},rgb:{supported:true},power_led:{supported:true,current:false}});
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{rgb:{enabled:true,ready:true}}},rgb_get_status:status(false)});
        panel.page="rgb";
        const row=findChild(panel,"field_rgb_Power button light");verify(row!==null);
        tryVerify(function(){ return row.visible && row.width>0; });
        const toggle=switchIn(row);verify(toggle!==null);verify(toggle.enabled);
        compare(toggle.checked,false,"Not managed yet: the switch shows the light, not the saved default");
        mouseClick(toggle,toggle.width/2,toggle.height/2);
        compare(fake.calls.length,1);compare(fake.calls[0].method,"rgb_set_power_led");compare(fake.calls[0].args[0],true);
        fake.data=Object.assign({},fake.data,{rgb_get_status:status(true)});
        compare(field("rgb","Power button light").get(),true,"Managed: the saved choice");
    }
    function test_whatDoesNotApplyToThisConsoleIsHidden() {
        const all={blocked:false,modules:{tdp:{ready:true},fan:{ready:true},vibration:{ready:true},rgb:{ready:true},remap:{ready:true},controller:{ready:true},battery:{ready:true},display:{ready:true},wifi:{ready:true}},
            device:{console:"go2",firmware:true,power_led:true}};
        const goS={blocked:false,modules:{fan:{applicable:false},display:{applicable:false},remap:{applicable:false},vibration:{applicable:false},controller:{applicable:false},
            rgb:{ready:true},battery:{ready:true},wifi:{ready:true},tdp:{ready:true}},device:{console:"gos",firmware:false,power_led:false}};
        const hidden=["Fan Control","Vibration","Button mapping","Gyro & touchpad","Display"],kept=["TDP","Lighting","Battery","Wi-Fi"];
        fake.data=Object.assign({},fake.data,{get_version:all,rgb_get_status:{settings:{control_enabled:true},rgb:{supported:true},power_led:{supported:true,current:true}}});
        panel.page="controller";
        compare(panel.cards.map(card=>card.owner).join(","),"vibration,remap,controller,firmware");
        for(const name of hidden.concat(kept))verify(field("modules",name).visible(),name);
        compare(field("modules","Lighting").note(),"Stick lights and power button light.");
        verify(field("rgb","Power button light").visible());
        verify(field("battery","Controllers").visible());
        fake.data=Object.assign({},fake.data,{get_version:goS});
        compare(panel.cards.length,0,"Neither the controller modules nor controller firmware apply");
        for(const name of hidden)verify(!field("modules",name).visible(),name);
        for(const name of kept)verify(field("modules",name).visible(),name);
        compare(field("modules","Lighting").note(),"Stick lights.");
        verify(!field("rgb","Power button light").visible());
        verify(!field("battery","Controllers").visible());
        panel.page="rgb";
        const row=findChild(panel,"field_rgb_Power button light");verify(row!==null);
        const stick=findChild(panel,"field_rgb_Stick lights");verify(stick!==null);
        tryVerify(function(){ return stick.visible && stick.width>0; });
        verify(!row.visible);
        fake.data=Object.assign({},fake.data,{get_version:all});
        // The cards are rebuilt when the controller modules return, so the row is looked up again.
        tryVerify(function(){ const again=findChild(panel,"field_rgb_Power button light");return again!==null && again.visible; });
    }
    function test_newChargerProfileIsSavedBeforeChargerCpuSettingsUnlock() {
        const cpu={available:true,cpu_boost:{available:true,enabled:false},epp:{available:true,value:"77",numeric_supported:true,profiles:[]},
            profile:{app_id:"42",ac_profile:true,active:false,cpu_boost_enabled:false,epp:"77"}};
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{tdp:{enabled:true,ready:true}}},
            get_settings:{enabled:true,spl:25000,sppt:28000,fppt:35000,active_preset:"balanced"},
            get_game_profile:{exists:false,active:false,profile:{},ac_profile:{},ac_separate:false},get_power_source:{ac:true},get_cpu_power_controls:cpu});
        field("tdp","Settings for this game").set("charger");
        compare(panel.tdpScope,"ac");
        compare(fake.calls.length,1,"The new profile and its charger split are saved right away");
        compare(fake.calls[0].method,"set_game_profile_active");compare(fake.calls[0].args.join(","),"42,true");
        compare(fake.calls[0].then.method,"set_game_ac_profile");compare(fake.calls[0].then.args.join(","),"42,25000,28000,35000,true,,false");
        const epp=findChild(panel,"field_tdp_CPU energy preference");verify(epp!==null);
        tryVerify(function(){ return epp.visible; });
        verify(!epp.enabled,"Charger CPU settings wait for the saved split");
        verify(!panel.fieldEnabled("tdp",field("tdp","CPU Boost")));
        fake.data=Object.assign({},fake.data,{get_game_profile:gameProfile({ac_separate:true})});
        tryVerify(function(){ return epp.enabled; });
        epp.edited(80);
        compare(fake.calls[1].method,"set_epp");compare(fake.calls[1].args.join(","),"204,42,true,42,false");
    }
    function test_controllerTestNeedsOnlyThePhysicalController() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{controller:{enabled:true,ready:true}}},
            controller_get_status:{available:false,reason:"InputPlumber is not running.",physical:{path:"/dev/hidraw3"},virtual:null}});
        panel.page="controller";
        const row=findChild(panel,"field_controller_Controller test");verify(row!==null);
        tryVerify(function(){ return row.visible && row.width>0; });
        verify(row.enabled,"The physical HID test works without InputPlumber");
        verify(!panel.fieldEnabled("controller",field("controller","Gyro source")),"Gyro routing still needs InputPlumber");
        fake.data=Object.assign({},fake.data,{controller_get_status:{available:false,physical:null,virtual:null}});
        verify(!panel.fieldEnabled("controller",field("controller","Controller test")),"No physical controller: no test");
    }
    function test_discardConfirmedAfterTheGameChangedDoesNotTouchTheNewGame() {
        fake.data=Object.assign({},fake.data,{get_game_profile:gameProfile()});
        panel.tdpScope="game";panel.loadPower();panel.editPower("spl",18);
        field("tdp","Settings for this game").set("global");
        compare(fake.calls.length,0);
        // Steam switches to another game while the dialog is open, and its fresh profile arrives.
        fake.gameProfileApp="7";
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"7"},get_game_profile:gameProfile()});
        fake.changed("desktop_context");fake.changed("get_game_profile");
        confirmDialog().accept();
        compare(fake.calls.length,0,"The choice made for game 42 never reaches game 7");
        verify(fake.error.length>0);
    }
    function firmware(phase,extra) {
        return Object.assign({phase:phase,message:"",error:"",token:"",install_enabled:true,install_reason:"",components:[
            {id:"left",label:"Left controller",installed:"260310A",available:"260401A",newer:true,downloaded:phase==="downloaded"},
            {id:"right",label:"Right controller",installed:"260310A",available:"260310A",newer:false,downloaded:false},
            {id:"receiver",label:"Receiver",installed:"260422A",available:"260422A",newer:false,downloaded:false}]},extra || {});
    }
    function test_firmwareButtonFollowsTheExplicitSteps() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("idle")});
        const fields=allFields("controller","firmware");
        const buttons=fields.filter(f=>f.kind==="button");
        compare(buttons.length,1);
        const step=buttons[0];
        compare(step.get(),"Check for updates");verify(step.available());
        step.set();compare(fake.calls[0].method,"firmware_check");
        fake.data.firmware_get_status=firmware("checking");
        compare(step.get(),"Checking…");verify(!step.available());
        fake.data.firmware_get_status=firmware("available");
        compare(step.get(),"Download");compare(panel.firmwareRow("left"),"Installed 260310A · Available 260401A · newer");
        step.set();compare(fake.calls[1].method,"firmware_download");
        fake.data.firmware_get_status=firmware("installing");
        compare(panel.firmwarePhase(),"Installing update");verify(!step.available());
        fake.data.firmware_get_status=firmware("failed",{error:"fwupd did not complete the update."});
        compare(step.get(),"Check for updates");verify(step.available());
        compare(panel.firmwareMessage(),"fwupd did not complete the update.");
    }
    function test_firmwareInstallNeedsConfirmationAndTheDownloadToken() {
        panel.page="controller";
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("downloaded",{token:"abc"})});
        const step=allFields("controller","firmware").find(f=>f.kind==="button");
        compare(step.get(),"Install");verify(step.available());
        step.set();compare(fake.calls.length,0);
        compare(panel.pendingMethod,"firmware_install");compare(panel.pendingArgs[0],"abc");
        verify(panel.confirmation.indexOf("Left controller: 260310A → 260401A")===0);
        verify(panel.confirmation.indexOf("AC power")>0);
        compare(confirmDialog().title,"Install controller firmware?");compare(confirmDialog().okText,"Install");confirmDialog().close();
        fake.data.firmware_get_status=firmware("downloaded",{token:"abc",install_enabled:false,install_reason:"Disabled in this build."});
        compare(step.get(),"Check for updates");verify(step.available());compare(panel.firmwareInstallReason(),"Disabled in this build.");
        step.set();compare(fake.calls.length,1);compare(fake.calls[0].method,"firmware_check");
        fake.data.firmware_get_status=firmware("downloaded",{token:""});
        verify(!step.available());
    }
    function test_firmwareButtonIsRenderedWhenControllerModuleIsDisabledOrPaused() {
        fake.data=Object.assign({},fake.data,{
            get_version:{blocked:false,modules:{controller:{enabled:false,ready:false}}},
            firmware_get_status:firmware("idle")
        });
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Firmware update");
        verify(button!==null);
        tryCompare(button,"visible",true);verify(button.enabled);
        mouseClick(button);compare(fake.calls[0].method,"firmware_check");
        fake.data=Object.assign({},fake.data,{
            get_version:{blocked:false,modules:{controller:{enabled:true,ready:false,maintenance:true}}},
            firmware_get_status:firmware("installing")
        });
        verify(button.visible);tryCompare(button,"enabled",false);
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("up_to_date")});
        tryCompare(button,"enabled",true);
    }
    function test_firmwareFailureOrStaleStatusCannotReuseOldIdleState() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("idle",{message:"Ready"})});
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Firmware update");
        verify(button!==null);verify(button.enabled);
        fake.readErrors={firmware_get_status:"Connection lost"};
        tryCompare(button,"enabled",false);
        fake.readErrors={};fake.firmwareAge=9;
        verify(!button.enabled);
        fake.firmwareAge=0;tryCompare(button,"enabled",true);
        fake.data=Object.assign({},fake.data,{firmware_get_status:{}});
        verify(!button.enabled);
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("idle",{message:{malformed:true}})});
        verify(!button.enabled);
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("toString")});
        verify(!button.enabled);
    }
    function test_nullFirmwareStatusCannotKeepTheRenderedButtonEnabled() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("idle")});
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Firmware update");
        verify(button!==null);verify(button.enabled);
        fake.data=Object.assign({},fake.data,{firmware_get_status:null});
        tryCompare(button,"enabled",false);
        compare(panel.firmwarePhase(),"Status unavailable");
        mouseClick(button);compare(fake.calls.length,0);
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("up_to_date")});
        tryCompare(button,"enabled",true);
    }
    function test_invalidModuleGateCannotKeepFirmwareActionEnabled() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:firmware("idle")});
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Firmware update");
        verify(button!==null);verify(button.enabled);
        for(const invalid of [null,{},[],false,{blocked:"false"}]) {
            fake.data=Object.assign({},fake.data,{get_version:invalid});
            tryCompare(button,"enabled",false);
            mouseClick(button);compare(fake.calls.length,0);
        }
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{}}});
        tryCompare(button,"enabled",true);
    }
    function test_combinedControllerKeepsIndependentModuleGatesAndRpcRouting() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{vibration:{ready:true},rgb:{ready:true},remap:{ready:true},controller:{ready:false}}},
            firmware_get_status:{phase:"idle"},
            vibe_is_ready:{ready:true},vibe_get_settings:{settings:{},app_id:"0",profile_id:"0"},
            rgb_get_status:{settings:{control_enabled:true},rgb:{supported:true}},remap_get_status:{supported:true}});
        panel.page="controller";
        compare(panel.cards.map(card=>card.owner).join(","),"vibration,remap,controller,firmware","Firmware stays last");
        const firmware=panel.cards.find(c=>c.owner==="firmware");
        verify(firmware!==undefined);
        panel.page="rgb";compare(panel.cards.map(card=>card.owner).join(","),"rgb");panel.page="controller";
        verify(panel.fieldEnabled("firmware",firmware.fields.find(f=>f.kind==="button")));
        const vibration=field("vibration","Vibration strength");
        const lighting=field("rgb","Enable lighting control");
        const mapping=field("remap","Enable button mapping");
        verify(panel.fieldEnabled("vibration",vibration));verify(panel.fieldEnabled("rgb",lighting));
        verify(!panel.fieldEnabled("controller",field("controller","Gyro source")));
        lighting.set(false);mapping.set(true);vibration.set(3);
        compare(fake.calls.map(c=>c.method).join(","),"rgb_set_control_enabled,remap_set_enabled,vibe_set_intensity");
        compare(fake.calls[2].args.join(","),"3,0,0");
        fake.busy=true;verify(!panel.fieldEnabled("rgb",lighting));fake.busy=false;
        fake.connected=false;verify(!panel.fieldEnabled("vibration",vibration));
    }
    function test_eppSliderUsesSelectedProfileAndGuardedNumericRpc() {
        fake.data=Object.assign({},fake.data,{get_cpu_power_controls:{epp:{available:true,numeric_supported:true,value:"balance_power",numeric_value:191},profile:{app_id:"42",ac_profile:true,epp:"64"}},
            get_game_profile:gameProfile({ac_separate:true})});
        panel.tdpScope="ac";
        compare(panel.eppPercent(),25);
        const slider=field("tdp","CPU energy preference");
        verify(slider.available());verify(slider.visible());slider.set(40);
        compare(fake.calls[0].method,"set_epp");compare(fake.calls[0].args.join(","),"102,42,true,42,false");
    }
    function test_controllerTestAndDetailsAreReadableRowsNotJson() {
        fake.data=Object.assign({},fake.data,{controller_get_status:{available:true,gyro_source:"left",applied_source:"left",
            physical:{path:"/dev/hidraw3"},virtual:{path:"/dev/input/event9"},imu:{available:true,actual:{left:true,right:false}}}});
        compare(field("controller","Gyro reporting").get(),"left On · right Off");
        compare(field("controller","Steam controller").get(),"/dev/input/event9");
        verify(!field("controller","Left gyro").visible(),"Test rows appear only while the test runs");
        compare(field("controller","Controller test").get(),"Start controller test");
        fake.diagnosticToken="tok";
        fake.data=Object.assign({},fake.data,{controller_get_diagnostics:{token:"tok",active:true,remaining_s:12.4,
            physical:{received:true,reports:120,rate_hz:125.4,sample:{gyro_left:{x:1,y:-2,z:3},gyro_right:null,touchpad:{is_touching:true,x:0.5,y:0.25}}},
            virtual:{received:false,reports:0,rate_hz:null,sample:null}}});
        compare(field("controller","Controller test").get(),"Stop test · 13 s left");
        const left=field("controller","Left gyro");verify(left.visible());
        compare(left.get(),"X 1 · Y -2 · Z 3");compare(left.note(),"120 reports · 125.4 Hz","Decky's rate format");
        compare(field("controller","Right gyro").get(),"—");
        compare(field("controller","Steam gyro").note(),"No reports yet.");
        compare(field("controller","Touchpad").get(),"Physical: touch at 0.50,0.25 · Steam: —");
        for(const entry of allFields("controller"))
            if(entry.kind==="text")verify(String(entry.get()).indexOf("{")<0,entry.label+" shows no raw JSON");
        field("controller","Controller test").set();
        compare(fake.calls[0].method,"controller_stop_diagnostics");compare(fake.calls[0].args[0],"tok");
        fake.diagnosticToken="";
    }
    function test_rgbSlidersPreserveOtherColorComponentAndRecoveryGate() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{rgb:{ready:true}}},rgb_get_status:{settings:{control_enabled:true,effect:"monocolor",hue:220,saturation:72},recovery_required:false}});
        const fields=allFields("rgb");
        fields.find(f=>f.label==="Hue").set(120);
        fields.find(f=>f.label==="Saturation").set(50);
        compare(fake.calls[0].args.join(","),"120,72");compare(fake.calls[1].args.join(","),"220,50");
        verify(!fields.find(f=>f.label==="Speed").visible());
        fake.data=Object.assign({},fake.data,{rgb_get_status:{settings:{control_enabled:true},recovery_required:true}});
        verify(!panel.fieldEnabled("rgb",field("rgb","Enable lighting control")));
        verify(panel.fieldEnabled("rgb",field("rgb","Restore original lighting")),"a restore action: the backend recovers first or refuses");
    }
    function test_lightingEffectsWaitForTheStickLights() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{rgb:{ready:true}}},
            rgb_get_status:{rgb:{supported:true,drift:true},settings:{control_enabled:true,rings_enabled:false,effect:"breathe",speed:75},power_led:{supported:false,reason:"No light."}}});
        for(const label of ["Effect","Hue","Saturation","Brightness","Speed"])verify(!panel.fieldEnabled("rgb",field("rgb",label)),label+" waits for the stick lights");
        compare(field("rgb","Stick lights").note(),"Restoring saved state…");
        compare(field("rgb","Power button light").note(),"No light.");
        compare(field("rgb","Speed").get(),100,"Decky's speed steps");
        fake.data=Object.assign({},fake.data,{rgb_get_status:{rgb:{supported:true},settings:{control_enabled:true,rings_enabled:true,effect:"breathe"}}});
        verify(panel.fieldEnabled("rgb",field("rgb","Hue")));
    }
    function test_batteryFollowsTheSharedRules() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{battery:{ready:true}}},
            battery_get_status:{supported:true,managed:true,enabled:false,requested_enabled:true,capacity:150},
            battery_get_controller_levels:{left:80,right:-5,connection_left:"detached"}});
        const toggle=field("battery","Battery protection");
        verify(toggle.get(),"the saved choice while Companion manages charging");verify(toggle.available());
        compare(toggle.note(),"Stops charging at about 80% to protect the battery. Waiting for the battery to confirm…");
        compare(field("battery","Battery").get(),"—");compare(field("battery","Controllers").get(),"left 80% (detached) · right —");
        fake.data=Object.assign({},fake.data,{battery_get_status:{supported:true,managed:false,enabled:null}});
        verify(!toggle.available(),"an unknown actual state locks the switch");
    }
    function test_vibrationNamesAndTouchpadStrength() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{vibration:{ready:true}}},vibe_is_ready:{ready:true},
            vibe_get_settings:{settings:{touchpadEnabled:false}},vibe_get_capabilities:{mode:[]}});
        compare(field("vibration","Vibration strength").choices().map(c=>c.label).join(),"Off,Low,Medium,High");
        compare(field("vibration","Vibration mode").choices().map(c=>c.label).join(),"FPS,Racing,Standard,SPG,RPG");
        verify(!field("vibration","Touchpad strength").available(),"touchpad strength waits for touchpad vibration");
    }
    function test_remapChoicesWaitForMapping() {
        fake.data=Object.assign({},fake.data,{remap_get_status:{supported:true,enabled:false,active:false,actions:[{id:"default",label:"Default"}]}});
        verify(!field("remap","Desktop button").available());verify(!field("remap","Page button").available());
        verify(field("remap","Restore defaults").visible());
        fake.data=Object.assign({},fake.data,{remap_get_status:{supported:true,enabled:true,active:false,actions:[]}});
        verify(field("remap","Desktop button").available());
        compare(field("remap","Enable button mapping").note(),"Change what the two extra buttons on the left controller do. Waiting to reapply…");
        fake.data=Object.assign({},fake.data,{remap_get_status:{supported:false,enabled:true}});
        verify(!field("remap","Page button").visible());verify(!field("remap","Restore defaults").visible());
    }
    function test_gyroSourceIsConfirmedByTheBackendAndWaitsForTheTest() {
        fake.data=Object.assign({},fake.data,{controller_get_status:{available:true,gyro_source:"left",applied_source:"left",controlled:false}});
        const source=field("controller","Gyro source");
        verify(source.note().endsWith(" Not confirmed yet…"),"matching filters alone are not confirmed routing");
        verify(source.available());
        fake.diagnosticToken="tok";verify(!source.available(),"no gyro change during the controller test");fake.diagnosticToken="";
        fake.data=Object.assign({},fake.data,{controller_get_status:{available:true,gyro_source:"left",controlled:true}});
        verify(source.note().indexOf("Not confirmed")<0);
    }
    function test_displayPausesOnSettingsErrorOrUnfinishedReset() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{display:{ready:true}}},
            display_get_state:{settings_error:"Settings unreadable.",panel_mode:"pq",setup_done:true,panel_ok:true,edid_fix:true}});
        compare(card("Display fix").fields[0].get(),"Display controls are paused. Settings unreadable.");
        verify(!panel.fieldEnabled("display",field("display","Fix HDR brightness in games")));
        verify(!panel.fieldEnabled("display",displayField("Brightness slider in HDR")));
        verify(!panel.fieldEnabled("display",displayField("Reset display fix")),"a reset cannot read unreadable settings either, as in Decky");
        fake.data=Object.assign({},fake.data,{display_get_state:{reset_in_progress:true,reset_error:"",panel_mode:"pq"}});
        const reset=displayField("Reset display fix");compare(reset.get(),"Retry display reset");
        reset.set();compare(fake.calls[0].method,"display_reset_settings","finishing a reset asks nothing more, as in Decky");
        fake.data=Object.assign({},fake.data,{display_get_state:{setup_done:false,panel_mode:null,reset_restart_pending:true}});
        verify(!displayField("Reset display fix").visible(),"nothing to reset before a mode is chosen");
        verify(!displayField("Display mode").available(),"first-run choices wait for the restart that finishes a reset");
        fake.data=Object.assign({},fake.data,{display_get_state:{setup_done:true,panel_mode:"pq",panel_ok:true,restart_pending:true}});
        verify(!displayField("Brightness slider in HDR").visible(),"not while a restart is pending");
    }
    function test_nativeDisplayHidesLegacyControlsButKeepsIndependentEdid() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{display:{ready:true}}},display_get_state:{native_support_detected:true,native_support_reason:"Gamescope includes the native Legion Go 2 display profile. Companion's display fix is no longer needed.",edid_fix:true}});
        panel.page="display";
        const upstream=card("Display"),legacy=card("Display fix");
        verify(upstream.visible());verify(!legacy.visible());
        compare(upstream.fields[1].get(),"SteamOS supports this screen. Companion's display fix is off. The HDR game fix below still works.");
        const edid=field("display","Fix HDR brightness in games");verify(edid.available());verify(panel.fieldEnabled("display",edid));
        edid.set(false);compare(fake.calls[0].method,"display_set_edid_fix");compare(fake.calls[0].args[0],false);
        fake.data=Object.assign({},fake.data,{display_get_state:{native_support_detected:true,native_support_reason:"Native support was detected earlier."}});
        compare(upstream.fields[1].get(),"Native support was detected earlier.");verify(!legacy.visible());
        fake.data=Object.assign({},fake.data,{display_get_state:{native_support_detected:false,setup_done:true}});
        verify(!upstream.visible());verify(legacy.visible());verify(field("display","Brightness slider in HDR").available());
    }
    function wifiState(settings,live,extra) {
        fake.data=Object.assign({},fake.data,{wifi_get_status:Object.assign({success:true,connected:true,
            settings:Object.assign({device_family:"legion_go_2",driver:"mt7921e",band_policy:"off"},settings),
            live:Object.assign({wifi_backend:"iwd",connection_state:"connected",connection_uuid:"home",frequency:"5180"},live)},extra || {})});
    }
    function test_wifiBandChoicesFollowTheSharedRules() {
        const band=field("wifi","Band"),ids=()=>band.choices().map(o=>o.id).join(",");
        wifiState({},{wifi_backend:"wpa_supplicant",six_ghz_only_supported:false});
        compare(ids(),"off");verify(!band.available(),"one choice: nothing to pick");
        wifiState({},{wifi_backend:"wpa_supplicant",six_ghz_only_supported:true});
        compare(ids(),"off,six_ghz_only");verify(band.available());
        wifiState({band_policy:"six_ghz_only"},{wifi_backend:"wpa_supplicant",six_ghz_only_supported:false});
        compare(ids(),"off,six_ghz_only");
        wifiState({},{});
        compare(ids(),"off,five_six_no_24,five_six_only");
        wifiState({},{},{connected:false});
        compare(ids(),"off,five_six_no_24","Only 5/6 GHz needs a live connection, as in Decky");
        wifiState({band_policy:"five_six_only"},{recovery_required:true,connection_state:"unknown"},{connected:false});
        compare(ids(),"off,five_six_only","Automatic stays available to restore");
        wifiState({},{wifi_backend:""});verify(!band.visible(),"no backend, no band choice");
    }
    function test_wifiReconnectNeedsAConnectionAndSharedTexts() {
        wifiState({band_policy:"five_six_no_24"},{},{connected:false});
        const reconnect=field("wifi","Reconnect to 5/6 GHz");verify(reconnect.visible());verify(!reconnect.available());
        reconnect.set();compare(fake.calls.length,0);
        wifiState({},{wifi_backend:"wpa_supplicant"},{connected:false});
        verify(!field("wifi","Connect to 6 GHz once").available());
        wifiState({},{wifi_backend:"wpa_supplicant",ssid:"Home",channel:"36",frequency:"5180"});
        verify(field("wifi","Connect to 6 GHz once").available());
        compare(field("wifi","Network").get(),"Home");compare(field("wifi","Network").note(),"5 GHz · channel 36");
        compare(field("wifi","Frequency").get(),"5180 MHz");compare(field("wifi","Backend").get(),"WPA supplicant");
        wifiState({band_policy:"six_ghz_only"},{wifi_backend:"wpa_supplicant",frequency:"5180"});
        compare(field("wifi","Band").note(),"Saved: Only 6 GHz, but connected on 5 GHz.");
        const notice=allFields("wifi")[0];
        wifiState({},{recovery_required:true,recovery_errors:["Rollback failed"]});
        compare(notice.get(),"Wi-Fi settings are locked. Rollback failed");
        wifiState({driver:"iwlwifi"},{});
        compare(notice.get(),"Wi-Fi tweaks unavailable. Only for the Legion Go Wi-Fi card (MT7922).");
    }
    function test_wifiChangeIsVerifiedInTheNextStatus() {
        wifiState({},{});
        field("wifi","Band").set("five_six_no_24");compare(fake.calls[0].args[0],"five_six_no_24");
        fake.mutationDone("wifi_set_band_policy",{success:true},true);
        fake.changed("wifi_get_status");
        compare(fake.error,"The final connection or setting could not be verified. Check the current Wi-Fi state.");
        fake.error="";field("wifi","Band").set("five_six_no_24");
        fake.mutationDone("wifi_set_band_policy",{success:true},true);
        wifiState({band_policy:"five_six_no_24"},{});fake.changed("wifi_get_status");
        compare(fake.error,"");
    }
    function test_unknownExtendedLimitsStateIsOffAndKeepsStandardCaps() {
        fake.data=Object.assign({},fake.data,{get_caps:{min:5,extras:true,tdp_available:true,presets:{},
            std:{spl:30,sppt:35,fppt:40},max:{spl:45,sppt:50,fppt:55}}});
        // get_extras_unlocked has not arrived (or failed): state() answers {}.
        const extended=field("tdp","Allow up to 50 W");
        compare(extended.get(),false);compare(panel.powerLimit("spl"),30);
        fake.data=Object.assign({},fake.data,{get_extras_unlocked:true});
        compare(extended.get(),true);compare(panel.powerLimit("spl"),45);
    }
    function confirmDialog() { const dialog=findChild(panel,"confirmDialog");verify(dialog!==null);return dialog; }
    function test_allowing50WattsAsksFirstButTurningItOffDoesNot() {
        fake.data=Object.assign({},fake.data,{get_caps:{min:5,extras:false,helper_retry_available:true,tdp_available:true,presets:{},
            std:{spl:30,sppt:35,fppt:40},max:{spl:30,sppt:35,fppt:40}},get_extras_unlocked:false});
        const extended=field("tdp","Allow up to 50 W");verify(extended.visible(),"Offered while the helper still has to be downloaded");
        extended.set(true);compare(fake.calls.length,0,"No RPC before the user confirms");
        const dialog=confirmDialog();
        compare(dialog.title,"Allow up to 50 W?");compare(dialog.okText,"Allow 50 W");
        verify(panel.confirmation.indexOf("beyond Lenovo's power limits")>0);
        verify(panel.confirmation.indexOf("downloads the verified RyzenAdj helper")>0);
        dialog.reject();compare(fake.calls.length,0);
        extended.set(true);dialog.accept();
        compare(fake.calls[0].method,"set_extras_unlocked");compare(fake.calls[0].args[0],true);
        dialog.close();extended.set(false);
        compare(fake.calls[1].method,"set_extras_unlocked");compare(fake.calls[1].args[0],false);
    }
    function test_deletingAGameProfileAsksFirst() {
        fake.data=Object.assign({},fake.data,{get_game_profile:{exists:true,profile:{spl:20000,sppt:25000,fppt:30000}}});
        const remove=field("tdp","Delete profile for this game");verify(remove.visible());
        remove.set();compare(fake.calls.length,0);
        const dialog=confirmDialog();
        compare(dialog.title,"Delete profile for this game?");compare(dialog.okText,"Delete");verify(dialog.danger);
        dialog.accept();compare(fake.calls[0].method,"delete_game_profile");compare(fake.calls[0].args[0],"42");
        fake.data=Object.assign({},fake.data,{get_game_profile:{exists:false}});verify(!remove.visible());
    }
    function test_strictWifiBandsAskButOtherBandChangesAndReconnectDoNot() {
        wifiState({},{});
        const band=field("wifi","Band");
        band.set("five_six_no_24");compare(fake.calls[0].method,"wifi_set_band_policy");compare(fake.calls[0].args[0],"five_six_no_24");
        band.set("five_six_only");compare(fake.calls.length,1);compare(confirmDialog().title,"Use only 5/6 GHz?");
        confirmDialog().accept();compare(fake.calls[1].args[0],"five_six_only");compare(fake.calls[1].args[1],true);confirmDialog().close();
        band.set("six_ghz_only");compare(fake.calls.length,2,"Only 6 GHz is not offered with iwd");
        wifiState({},{wifi_backend:"wpa_supplicant",six_ghz_only_supported:true});
        band.set("six_ghz_only");compare(fake.calls.length,2);compare(confirmDialog().title,"Use only 6 GHz?");
        confirmDialog().accept();compare(fake.calls[2].args[0],"six_ghz_only");compare(fake.calls[2].args[1],false);confirmDialog().close();
        wifiState({band_policy:"five_six_no_24"},{});
        field("wifi","Reconnect to 5/6 GHz").set();compare(fake.calls[3].method,"wifi_rescan_and_reconnect");
    }
    function test_displayResetAndRestartUseTheSharedConfirmations() {
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"",gamescope:true},display_get_state:{setup_done:true,panel_mode:"hybrid"}});
        field("display","Reset display fix").set();compare(fake.calls.length,0);
        compare(confirmDialog().title,"Reset display fix?");verify(confirmDialog().danger);confirmDialog().accept();
        compare(fake.calls[0].method,"display_reset_settings");confirmDialog().close();
        field("display","Restart Gaming Mode").set();compare(confirmDialog().title,"Restart Gaming Mode?");compare(confirmDialog().okText,"Restart");
        confirmDialog().close();
        field("display","Install display fix").set();compare(confirmDialog().title,"Install display fix for Hybrid?");
        confirmDialog().accept();compare(fake.calls[1].method,"display_run_setup");compare(fake.calls[1].args[0],"hybrid");
    }
    function displayField(name) {
        const entry=card("Display fix").fields.find(item=>(item.name || item.label)===name);
        verify(entry!==undefined,"Display field: "+name);return entry;
    }
    function test_displayFollowsDeckyCompactModeChoiceAndBrightnessReason() {
        const reason="hybrid mode - the panel switches itself, nothing to forward";
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:""},display_get_state:{setup_done:true,panel_mode:"hybrid",active:false,nits:0,enabled:true,
            gamescope_reachable:true,hdr_now:false,reason:reason,panel_desc:"Panel"}});
        compare(displayField("Brightness").get(),reason,"Nothing forwarded is not 0 nits");
        compare(displayField("Brightness slider in HDR").note(),"Lets the Steam brightness slider dim the screen in PQ. Not needed in the current mode.");
        const change=displayField("Change display mode"),modes=displayField("Display mode");
        compare(change.note(),"Now: Hybrid (screen in Gamma 2.2)");compare(change.get(),"Change display mode");
        verify(!modes.visible(),"The other modes stay folded away");
        change.set();verify(modes.visible());compare(change.get(),"Cancel");
        compare(modes.choices().map(c=>c.label).join(),"Use PQ,Use Gamma 2.2");
        verify(modes.choices().every(c=>c.detail.indexOf("+ ")===0 && c.detail.indexOf("
− ")>0));
        modes.set("pq");compare(fake.calls[0].method,"display_set_panel_mode");compare(fake.calls[0].args[0],"pq");
        verify(!modes.visible(),"Picking a mode folds the list again");
        verify(!displayField("Restart Gaming Mode").visible(),"Restart only when one is pending");
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"",gamescope:true},display_get_state:{setup_done:false,panel_mode:null}});
        verify(modes.visible());verify(!change.visible());verify(!displayField("Install display fix").visible());
        compare(modes.choices()[0].label,"Hybrid (recommended)");
        modes.set("gamma22");compare(fake.calls.length,1,"First setup installs a script: it asks first");
        compare(confirmDialog().title,"Install display fix for Gamma 2.2?");confirmDialog().accept();
        compare(fake.calls[1].method,"display_run_setup");compare(fake.calls[1].args[0],"gamma22");confirmDialog().close();
        fake.data=Object.assign({},fake.data,{display_get_state:{setup_done:true,panel_mode:"pq",restart_pending:true}});
        verify(displayField("Restart Gaming Mode").visible());
    }
}
