// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtTest
import "../.."
// The Custom sliders follow the Decky panel's model (src/tdp.tsx; tests/frontend_lifecycle.cjs).
TestCase {
    name:"TdpCustomLimits"; when:windowShown; width:1000;height:1000
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
        property string gameProfileApp:""
        property string editApp:""
        property var readErrors:({})
        signal changed(string method)
        signal mutationDone(string method,var result,bool success)
        function state(method) { return data[method]===undefined?{}:data[method]; }
        function now() { return 0; }
        function age(method) { return 0; }
        function issue(method,args) {}
        function mutate(method,args,then) { calls=calls.concat([{method:method,args:args,then:then || null}]); }
    }
    Component { id:factory; ModulePage { width:950;backend:fake;page:"tdp" } }
    property var panel
    function settings(advanced,spl,sppt,fppt) {
        return {enabled:true,spl:spl*1000,sppt:sppt*1000,fppt:fppt*1000,active_preset:"custom",advanced_tdp_control:advanced};
    }
    function start(advanced,spl,sppt,fppt,std) {
        fake.calls=[];
        fake.data={get_version:{blocked:false,modules:{tdp:{enabled:true,ready:true}}},desktop_context:{app_id:""},
            get_settings:settings(advanced,spl,sppt,fppt),get_extras_unlocked:false,get_game_profile:{exists:false},
            get_caps:{min:5,std:std || {spl:35,sppt:37,fppt:45},max:{spl:50,sppt:50,fppt:50},extras:true,tdp_available:true,
                presets:{silent:{spl:8,sppt:15,fppt:20},balanced:{spl:16,sppt:25,fppt:30},performance:{spl:20,sppt:32,fppt:35},max:{spl:35,sppt:37,fppt:45}}}};
        panel=createTemporaryObject(factory,this);verify(panel!==null);
    }
    function field(label) {
        const entry=panel.forms().tdp.reduce((all,card)=>all.concat(card.fields),[]).find(item=>(item.name || item.label)===label);
        verify(entry!==undefined,"Field: "+label);return entry;
    }
    function applied() {
        field("Custom limits").set("apply");
        const call=fake.calls[fake.calls.length-1];compare(call.method,"apply_tdp");
        return call.args.slice(0,3).map(v=>v/1000);
    }
    function limits() { const t=panel.powerDraft;return [t.spl,t.spl+t.spptOff,t.spl+t.fpptOff]; }
    // RPC lists reach QML as Qt sequences: array-like, but not Array.isArray().
    function sequence(values) { const result={length:values.length};values.forEach((v,i)=>result[i]=v);return result; }
    function test_firmwareRowsReadSequenceLists() {
        start(true,15,20,25);
        fake.data=Object.assign({},fake.data,{firmware_get_status:{phase:"available",components:sequence([{id:"left",installed:"1.0",available:"1.1",newer:true}])}});
        compare(panel.firmwareComponent("left").available,"1.1");
    }
    function test_gameProfilesListWhatEachGameUses() {
        start(true,15,20,25);
        compare(field("Game profiles").get(),[]);
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"20"},list_game_profiles:{profiles:sequence([
            {app_id:"20",active:true,ac_separate:false,preset:"balanced",spl:16000,ac_preset:"",ac_spl:16000},
            {app_id:"3000000001",active:true,ac_separate:true,preset:"custom",spl:12000,ac_preset:"performance",ac_spl:20000},
            {app_id:"10",active:false,ac_separate:false,preset:"",spl:15000,ac_preset:"",ac_spl:15000}])}});
        compare(field("Game profiles").get().map(row=>[row.id,row.title,row.detail,row.actions.map(a=>a.label+(a.enabled===false?"-off":"")).join("/")]),[
            ["10","App 10","Global · its settings are kept: 15 W","Edit/Delete"],
            ["20","App 20 · running","This game · Balanced · 16 W","Editing-off/Delete"],
            ["3000000001","Non-Steam game 3000000001","This game + charger · Battery: Custom · 12 W · Charger: Performance · 20 W","Edit/Delete"]]);
        const shown=findChild(panel,"field_tdp_Game profiles");verify(shown!==null);
        tryCompare(shown,"listCount",3);
    }
    function test_editingASavedProfileSavesItWithoutTheRunningGame() {
        start(true,15,20,25);
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"20"},
            list_game_profiles:{profiles:sequence([{app_id:"30",active:true,ac_separate:false,preset:"balanced",spl:16000,ac_preset:"",ac_spl:16000}])},
            get_game_profile:{exists:true,active:true,ac_separate:false,profile:{spl:16000,sppt:25000,fppt:30000,preset:"balanced"},ac_profile:{}}});
        field("Game profiles").set({id:"30",action:"edit"});
        compare([panel.app(),fake.editApp,panel.stored()],["30","30",true]);
        fake.gameProfileApp="30";fake.changed("get_game_profile");
        compare(panel.tdpScope,"game");
        panel.choosePreset("performance");
        let call=fake.calls[fake.calls.length-1];
        compare([call.method].concat(call.args.slice(3)),["apply_tdp","30","performance","20",true]);
        field("CPU Boost").set(false);
        call=fake.calls[fake.calls.length-1];
        compare([call.method].concat(call.args),["set_cpu_boost",false,"30",false,"20",true]);
        field("Game profiles").set({id:"30",action:"delete"});
        compare([panel.pendingMethod].concat(panel.pendingArgs),["delete_game_profile","30"]);
        // Once the profile is gone the editors return to the running game.
        fake.data=Object.assign({},fake.data,{list_game_profiles:{profiles:sequence([])}});fake.changed("list_game_profiles");
        compare([panel.app(),fake.editApp,panel.stored()],["20","",false]);
    }
    function test_discardingAGlobalDraftOpensASavedProfileWithoutARunningGame() {
        start(true,15,20,25);
        fake.data=Object.assign({},fake.data,{list_game_profiles:{profiles:sequence([{app_id:"30",active:true,ac_separate:false,preset:"balanced",spl:16000,ac_preset:"",ac_spl:16000}])}});
        fake.error="";panel.powerDirty=true;
        field("Game profiles").set({id:"30",action:"edit"});
        verify(panel.pendingAction!==null,"An unsaved draft asks first");
        panel.confirmed();
        compare([panel.app(),fake.editApp,fake.error],["30","30",""]);
    }
    function test_singleDialBoostFollowsTheStandardCapsUntil50WattsIsAllowed() {
        start(false,20,21,22);
        const spl=field("Sustained power");
        compare(spl.maximum(),35);compare(spl.note(),"Boost follows automatically.");
        verify(!field("Slow boost").visible());verify(!field("Fast boost").visible());
        compare(limits(),[20,30,35],"A saved Custom profile follows the single-dial rule");
        spl.set(35);compare(applied(),[35,37,45]);
        fake.data=Object.assign({},fake.data,{get_extras_unlocked:true});
        compare(spl.maximum(),50);
        spl.set(50);compare(applied(),[50,50,50]);
        fake.data=Object.assign({},fake.data,{get_settings:settings(true,20,21,22)});
        fake.mutationDone("set_advanced_tdp_control",{success:true},true);
        verify(field("Slow boost").visible());verify(field("Fast boost").visible());
        compare(limits(),[20,21,22],"Separate boost limits starts from the saved values");
    }
    function test_boostSlidersAreHeadroomThatDragEachOther() {
        start(true,20,21,22);
        const slow=field("Slow boost"),fast=field("Fast boost");
        compare(slow.get(),1);compare(fast.get(),2);compare(slow.maximum(),15);compare(fast.maximum(),15);
        compare(slow.note(),"Up to 21 W.");
        slow.set(10);compare(limits(),[20,30,30],"Pushing slow boost up drags fast boost along");
        fast.set(4);compare(limits(),[20,24,24],"Pulling fast boost below slow boost drags it down");
        slow.set(40);compare(limits(),[20,35,35],"Headroom stops at 15 W");
        field("Sustained power").set(35);compare(limits(),[35,37,45],"Raising SPL re-clamps both boosts to the ceilings");
        field("Sustained power").set(60);compare(panel.powerDraft.spl,35);
        compare(applied(),[35,37,45]);
    }
    function test_noRoomLeftDisablesTheBoostAtTheCeiling() {
        start(true,35,35,35,{spl:35,sppt:35,fppt:35});
        const slow=field("Slow boost");
        compare(slow.note(),"No room left at this power.");verify(!slow.available());compare(slow.maximum(),1);
        verify(!panel.fieldEnabled("tdp",slow));
        field("Sustained power").set(30);verify(slow.available());compare(slow.note(),"Up to 30 W.");
    }
    // Rules and texts from tdp.mjs, the same as in the Decky panel.
    function test_summaryWarnsAboveLenovosLimitOnlyWithout50Watts() {
        start(true,40,45,50);
        fake.data=Object.assign({},fake.data,{get_tdp_info:{success:true,values:{package_draw:18.25,spl_limit:40}}});
        compare(field("Enable TDP control").note(),"Global: Custom · 40 W · drawing 18.3 W · above Lenovo's limit");
        fake.data=Object.assign({},fake.data,{get_extras_unlocked:true});
        compare(field("Enable TDP control").note(),"Global: Custom · 40 W · drawing 18.3 W");
        fake.gameProfileApp="20";
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"20"},get_game_profile:{exists:true,active:true,profile:{}},
            get_settings:Object.assign(settings(true,40,45,50),{active_spl:16000,active_sppt:25000,active_fppt:30000})});
        compare(field("Enable TDP control").note(),"App 20: Balanced · 16 W · drawing 18.3 W","The game is named and its preset follows its values");
        fake.data=Object.assign({},fake.data,{get_settings:{enabled:false}});
        compare(field("Enable TDP control").note(),"Off · the console uses Lenovo's power limits.");
        fake.gameProfileApp="";
    }
    function test_helperNoticesAndRetryFollowTheMissingHelper() {
        start(true,15,20,25);
        const caps=extra=>Object.assign({},fake.data.get_caps,extra);
        fake.data=Object.assign({},fake.data,{get_caps:caps({helper_retry_available:true})});
        verify(!field("Retry helper download").visible(),"Nothing to retry while TDP and 50 W work");
        compare(panel.tdpNoticeText(),"");
        fake.data=Object.assign({},fake.data,{get_caps:caps({helper_retry_available:true,extras:false}),get_extras_unlocked:true});
        verify(field("Retry helper download").visible());
        compare(panel.tdpNoticeText(),"50 W paused. Firmware limits are active until the 50 W helper is available. Saved profiles are kept.");
        fake.data=Object.assign({},fake.data,{get_caps:caps({tdp_available:false})});
        compare(panel.tdpNoticeText(),"TDP control unavailable. Neither the firmware TDP interface nor the verified RyzenAdj helper is available.");
    }
    function test_failedProfileReadShowsARetry() {
        start(true,15,20,25);
        fake.data=Object.assign({},fake.data,{desktop_context:{app_id:"20"}});
        verify(!field("Retry profile").visible());
        fake.readErrors={get_game_profile:"Timed out."};
        compare(field("Could not load profile").get(),"Timed out.");verify(field("Retry profile").visible());
        verify(panel.fieldEnabled("tdp",field("Retry profile")),"Retry works while the profile is missing");
        fake.readErrors={};
    }
    function test_cpuControlsUseThePanelsLabelsAndRules() {
        start(true,15,20,25);
        const cpu=extra=>Object.assign({available:true,cpu_boost:{available:true,enabled:null},
            epp:{available:true,value:null,numeric_supported:true,profiles:["default","performance","balance_power","low-power"]}},extra || {});
        fake.data=Object.assign({},fake.data,{get_cpu_power_controls:cpu()});
        const boost=field("CPU Boost");
        verify(!boost.available(),"An unknown CPU Boost state cannot be toggled");compare(boost.note(),"The current state could not be read.");
        const slider=field("CPU energy preference");
        verify(slider.visible());compare(slider.get(),50);compare(slider.note(),"Set by the system. Move the slider to choose.");
        compare([slider.lowLabel,slider.highLabel],["Prefer CPU","Prefer GPU"]);
        fake.data=Object.assign({},fake.data,{get_cpu_power_controls:cpu({epp:{available:true,value:"balance_power",numeric_supported:false,profiles:["default","performance","balance_power","low-power"]}})});
        const list=field("CPU energy profile");
        verify(list.visible());verify(!slider.visible());compare(list.label,"CPU energy preference");
        compare(list.choices().map(c=>c.label),["System default","Prefer CPU","Balanced · Prefer GPU","Low power"]);
    }
    function test_noEditSequenceFormsAnInvalidConfiguration() {
        start(true,15,20,25);
        let seed=7;const next=()=>(seed=(seed*1103515245+12345)%2147483648)/2147483648;
        const names=["Sustained power","Slow boost","Fast boost"];
        for(let i=0;i<300;i++) {
            if(i===150)fake.data=Object.assign({},fake.data,{get_extras_unlocked:true});
            field(names[Math.floor(next()*3)]).set(Math.round(next()*60)-5);
            const [spl,sppt,fppt]=limits(),caps=panel.tdpCaps();
            verify(spl>=5 && spl<=sppt && sppt<=fppt,"order "+[spl,sppt,fppt]);
            verify(spl<=caps.spl && sppt<=caps.sppt && fppt<=caps.fppt,"ceilings "+[spl,sppt,fppt]);
        }
    }
}
