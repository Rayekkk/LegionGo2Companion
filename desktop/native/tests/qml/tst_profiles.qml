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
        function issue(method,args) { calls=calls.concat([{method:method,args:args}]); }
        function mutate(method,args) { calls=calls.concat([{method:method,args:args}]); }
    }
    Component { id:factory; ModulePage { width:950;backend:fake;page:"tdp" } }
    property var panel
    function init() {
        fake.calls=[];fake.error="";fake.connected=true;fake.busy=false;fake.readErrors={};fake.firmwareAge=0;
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
        mouseClick(cleanup,cleanup.width-30,cleanup.height/2);
        compare(fake.calls[0].method,"modules_set_enabled");compare(fake.calls[0].args[0],"fan");compare(fake.calls[0].args[1],false);
        const startup=findChild(panel,"field_modules_Retry Lighting start");verify(startup!==null);verify(startup.visible);
        mouseClick(startup,startup.width-30,startup.height/2);
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
    function test_inactiveGlobalSaveExplainsThatTheGameProfileStillApplies() {
        panel.editPower("spl",18);
        fake.mutationDone("apply_tdp",{success:true,saved_only:true,applied:false,
            message:"Global TDP settings saved. They will apply when no game profile is active."},true);
        compare(panel.powerDirty,false);
        compare(field("tdp","Power profile").get(),"Global TDP settings saved. They will apply when no game profile is active.");
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
        const level=field("vibration","Vibration level"),mode=field("vibration","Rumble mode"),touchpad=field("vibration","Touchpad intensity");
        compare(level.choices().length,4);compare(level.choices()[2].label,"Medium");
        compare(mode.choices().length,5);compare(mode.choices()[2].id,2);
        compare(touchpad.choices().length,4);
        level.set(3);compare(fake.calls[0].method,"vibe_set_intensity");
        compare(fake.calls[0].args[0],3);compare(fake.calls[0].args[1],"0");
    }
    function test_tdpPresetUsesAbsoluteBackendWattValues() {
        fake.data=Object.assign({},fake.data,{get_caps:{presets:{balanced:{spl:16,sppt:25,fppt:30}}}});
        field("tdp","Preset").set("balanced");
        compare(panel.powerDraft.spl,16);compare(panel.powerDraft.sppt,25);compare(panel.powerDraft.fppt,30);
        panel.applyPower();compare(fake.calls[0].args[0],16000);
        compare(fake.calls[0].args[1],25000);compare(fake.calls[0].args[2],30000);
        compare(fake.calls[0].args[4],"balanced");
    }
    function test_firmwareHasOneSharedActionAndNoComponentControls() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:{job:{phase:"idle",message:""}}});
        const fields=allFields("controller","firmware");
        const buttons=fields.filter(f=>f.kind==="button");
        compare(buttons.length,1);
        const check=fields.find(f=>f.label==="Check updates");
        verify(check.available());check.set();compare(fake.calls[0].method,"firmware_check_updates");
        fake.data.firmware_get_status.job.phase="installing";verify(!check.available());
        compare(panel.firmwarePhase(),"Updating controllers");
        fake.data.firmware_get_status.job.phase="uncertain";verify(!check.available());
        fake.data.firmware_get_status.job.phase="up_to_date";verify(check.available());
    }
    function test_firmwareButtonIsRenderedWhenControllerModuleIsDisabledOrPaused() {
        fake.data=Object.assign({},fake.data,{
            get_version:{blocked:false,modules:{controller:{enabled:false,ready:false}}},
            firmware_get_status:{job:{phase:"idle"}}
        });
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Check updates");
        verify(button!==null);
        tryCompare(button,"visible",true);verify(button.enabled);
        mouseClick(button);compare(fake.calls[0].method,"firmware_check_updates");
        fake.data=Object.assign({},fake.data,{
            get_version:{blocked:false,modules:{controller:{enabled:true,ready:false,maintenance:true}}},
            firmware_get_status:{job:{phase:"installing"}}
        });
        verify(button.visible);tryCompare(button,"enabled",false);
        fake.data=Object.assign({},fake.data,{firmware_get_status:{job:{phase:"up_to_date"}}});
        tryCompare(button,"enabled",true);
    }
    function test_firmwareFailureOrStaleStatusCannotReuseOldIdleState() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:{job:{phase:"idle",message:"Ready"}}});
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Check updates");
        verify(button!==null);verify(button.enabled);
        fake.readErrors={firmware_get_status:"Connection lost"};
        tryCompare(button,"enabled",false);
        fake.readErrors={};fake.firmwareAge=9;
        verify(!button.enabled);
        fake.firmwareAge=0;tryCompare(button,"enabled",true);
        fake.data=Object.assign({},fake.data,{firmware_get_status:{}});
        verify(!button.enabled);
        fake.data=Object.assign({},fake.data,{firmware_get_status:{job:{phase:"idle",message:{malformed:true}}}});
        verify(!button.enabled);
    }
    function test_nullFirmwareStatusCannotKeepTheRenderedButtonEnabled() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:{job:{phase:"idle"}}});
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Check updates");
        verify(button!==null);verify(button.enabled);
        fake.data=Object.assign({},fake.data,{firmware_get_status:null});
        tryCompare(button,"enabled",false);
        compare(panel.firmwarePhase(),"Status unavailable");
        mouseClick(button);compare(fake.calls.length,0);
        fake.data=Object.assign({},fake.data,{firmware_get_status:{job:{phase:"up_to_date"}}});
        tryCompare(button,"enabled",true);
    }
    function test_invalidModuleGateCannotKeepFirmwareActionEnabled() {
        fake.data=Object.assign({},fake.data,{firmware_get_status:{job:{phase:"idle"}}});
        panel.page="controller";
        const button=findChild(panel,"action_firmware_Check updates");
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
            firmware_get_status:{job:{phase:"idle"}},
            vibe_is_ready:{ready:true},vibe_get_settings:{settings:{},app_id:"0",profile_id:"0"},
            rgb_get_status:{settings:{control_enabled:true},rgb:{supported:true}},remap_get_status:{supported:true}});
        panel.page="controller";
        for(const owner of ["firmware","vibration","rgb","remap","controller"])verify(panel.cards.some(card=>card.owner===owner));
        const firmware=panel.cards.find(c=>c.owner==="firmware");
        verify(firmware!==undefined);
        verify(panel.fieldEnabled("firmware",firmware.fields.find(f=>f.label==="Check updates")));
        const vibration=field("vibration","Vibration level");
        const lighting=field("rgb","Control lighting");
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
        fake.data=Object.assign({},fake.data,{get_cpu_power_controls:{epp:{available:true,numeric_supported:true,value:"balance_power",numeric_value:191},profile:{app_id:"42",ac_profile:true,epp:"64"}}});
        panel.tdpScope="ac";
        compare(panel.eppPercent(),25);
        const slider=field("tdp","CPU energy preference");
        verify(slider.available());verify(slider.visible());slider.set(40);
        compare(fake.calls[0].method,"set_epp");compare(fake.calls[0].args.join(","),"102,42,true,42");
    }
    function test_rgbSlidersPreserveOtherColorComponentAndRecoveryGate() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{rgb:{ready:true}}},rgb_get_status:{settings:{control_enabled:true,effect:"monocolor",hue:220,saturation:72},recovery_required:false}});
        const fields=allFields("rgb");
        fields.find(f=>f.label==="Hue").set(120);
        fields.find(f=>f.label==="Saturation").set(50);
        compare(fake.calls[0].args.join(","),"120,72");compare(fake.calls[1].args.join(","),"220,50");
        verify(!fields.find(f=>f.label==="Animation speed").visible());
        fake.data=Object.assign({},fake.data,{rgb_get_status:{settings:{control_enabled:true},recovery_required:true}});
        verify(!panel.fieldEnabled("rgb",field("rgb","Control lighting")));
    }
    function test_nativeDisplayHidesLegacyControlsButKeepsIndependentEdid() {
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{display:{ready:true}}},display_get_state:{native_support_detected:true,native_support_reason:"Gamescope includes the native Legion Go 2 display profile. Companion's display fix is no longer needed.",edid_fix:true}});
        panel.page="display";
        const native=card("Display"),legacy=card("Display Fix");
        verify(native.visible());compare(native.badgeTone(),"green");verify(!legacy.visible());
        const edid=field("display","EDID brightness fix");verify(edid.available());verify(panel.fieldEnabled("display",edid));
        edid.set(false);compare(fake.calls[0].method,"display_set_edid_fix");compare(fake.calls[0].args[0],false);
        fake.data=Object.assign({},fake.data,{display_get_state:{native_support_detected:true,native_support_reason:"Native support was detected earlier."}});
        compare(native.badgeTone(),"amber");verify(!legacy.visible());
        fake.data=Object.assign({},fake.data,{display_get_state:{native_support_detected:false,setup_done:true}});
        verify(!native.visible());verify(legacy.visible());verify(field("display","Enable display correction").available());
    }

}
