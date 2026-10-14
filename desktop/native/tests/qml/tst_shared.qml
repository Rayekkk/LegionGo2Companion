// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtTest
import "../.."
TestCase {
    name:"SharedNativeState"; when:windowShown; width:1000; height:820
    QtObject {
        id:fake
        property int sequence:0
        property real clock:10
        property var calls:[]
        signal finished(int id,string method,var result,string problem)
        function monotonicSeconds() { return clock; }
        function cancelReads() {}
        function call(method,args,mutation) {
            const id=++sequence;
            calls=calls.concat([{id:id,method:method,args:args,mutation:mutation}]); return id;
        }
    }
    Component { id:modelFactory; Backend { transport:fake; windowActive:false } }
    Component { id:fieldFactory; UiField { width:500; label:"Power"; kind:"number"; minimum:5;maximum:5;value:15 } }
    Component { id:fanFactory; FanPage { width:950 } }
    property var model
    function init() {
        fake.calls=[]; fake.clock=10;
        model=createTemporaryObject(modelFactory,this); verify(model!==null);
        model.connected=true;
        model.snapshots={get_version:{blocked:false,modules:{tdp:{enabled:true,ready:true},fan:{enabled:true,ready:true},controller:{enabled:true,ready:true}}}};
    }
    function response(index,result,problem) { const request=fake.calls[index]; fake.finished(request.id,request.method,result,problem || ""); }
    function test_falseReadbackDoesNotBecomeTruthyObject() {
        model.issue("get_extras_unlocked");response(0,false);compare(model.state("get_extras_unlocked"),false);
    }
    function test_firmwareMutationsImmediatelyAdoptTheSharedJobSnapshot() {
        model.mutate("firmware_check_updates",[]);
        response(0,{job:{phase:"checking"}});
        compare(model.state("firmware_get_status").job.phase,"checking");
        verify(!model.busy);
        model.mutate("firmware_check_updates",[]);
        response(1,{job:{phase:"downloading",component:"left"}});
        compare(model.state("firmware_get_status").job.component,"left");
        compare(model.state("firmware_get_status").job.phase,"downloading");
    }
    function test_blockedModulesStillPollOnlyTheIndependentFirmwareOutcome() {
        model.page="controller";model.windowActive=true;
        model.snapshots={get_version:{blocked:true,modules:{}}};
        fake.calls=[];model.refreshPanel();
        compare(fake.calls.length,1);
        compare(fake.calls[0].method,"firmware_get_status");
        verify(!fake.calls[0].mutation);
        fake.calls=[];model.windowActive=false;model.refreshPanel();
        compare(fake.calls.length,0);
    }
    function test_firmwareMutationFailureRequiresFreshStatusBeforeAnotherAttempt() {
        model.snapshots={firmware_get_status:{job:{phase:"idle"}}};
        model.received={firmware_get_status:fake.clock};
        model.mutate("firmware_check_updates",[]);
        response(0,{},"Connection lost; result unknown.");
        verify(model.age("firmware_get_status")>=8);
        verify(!!model.readErrors.firmware_get_status);
        compare(fake.calls.filter(c=>c.method==="firmware_check_updates").length,1);
        model.issue("firmware_get_status");response(1,{job:{phase:"checking"}});
        compare(model.age("firmware_get_status"),0);
        compare(model.readErrors.firmware_get_status,"");
    }
    function test_obsoletePageCannotReplaceCurrentSnapshot() {
        model.issue("get_settings");model.generation++;
        response(0,{enabled:true});compare(model.state("get_settings").enabled,undefined);
    }
    function test_internalLeaseCannotUnlockPendingUserMutation() {
        model.mutate("set_plugin_enabled",[false]);verify(model.busy);
        model.issue("set_panel_active",[false],true);response(1,true);verify(model.busy);
        response(0,{success:true});verify(!model.busy);
    }
    function test_errorKeepsUnknownStateAndNeverRetriesWrite() {
        model.mutate("set_plugin_enabled",[false]);response(0,{},"Connection lost; state unknown.");
        verify(!model.busy);verify(model.error.length>0);wait(30);
        compare(fake.calls.filter(c=>c.method==="set_plugin_enabled").length,1);
    }
    function test_delayedTelemetryIncludesRequestLatency() {
        model.issue("fan_get_state");fake.clock=14;response(0,{readings:{valid:true}});compare(model.age("fan_get_state"),4);
    }
    function test_previousGameReadCannotBecomeCurrentProfile() {
        model.snapshots={desktop_context:{app_id:"99"}};
        model.issue("get_game_profile",["42"]);response(0,{exists:true,profile:{spl:30000}});
        compare(model.gameProfileApp,"");compare(model.state("get_game_profile").exists,undefined);
        model.issue("get_game_profile",["99"]);response(1,{exists:false});compare(model.gameProfileApp,"99");
    }
    function test_previousCpuScopeReadIsDiscarded() {
        model.cpuApp="99";model.cpuAc=true;
        model.issue("get_cpu_power_controls",["42",false]);response(0,{profile:{app_id:"42"}});
        compare(model.state("get_cpu_power_controls").profile,undefined);
    }
    function test_hiddenWindowIssuesNoPollAndReleasesItsLease() {
        model.windowActive=true;verify(fake.calls.length>0);
        model.windowActive=false;
        const size=fake.calls.length;model.refresh();compare(fake.calls.length,size);
        compare(fake.calls[size-1].method,"set_panel_active");compare(fake.calls[size-1].args[0],false);
    }
    function test_visibleOverviewRenewsTelemetryLeaseAndHiddenWindowReleasesIt() {
        model.page="overview";model.windowActive=true;
        fake.calls=[];model.refreshPanel();
        verify(fake.calls.some(call=>call.method==="set_panel_active" && call.args[0]===true));
        fake.calls=[];model.windowActive=false;
        verify(fake.calls.some(call=>call.method==="set_panel_active" && call.args[0]===false));
    }
    function test_spinboxRebindsWhenCapabilitiesArrive() {
        const field=createTemporaryObject(fieldFactory,this);verify(field!==null);
        const box=findChild(field,"nativeNumber");verify(box!==null);tryCompare(box,"value",5);
        field.maximum=30;tryCompare(box,"value",15);
    }
    function test_overviewKeepsSensorsFastAndOtherReadsBounded() {
        model.page="overview";
        model.overviewDetailsAt=-1000;
        const first=model.panelMethods();
        verify(first.indexOf("get_tdp_info")>=0);
        verify(first.indexOf("wifi_get_status")>=0);
        verify(first.indexOf("display_get_state")<0);
        verify(first.indexOf("controller_get_status")>=0);
        fake.clock=11;
        compare(model.panelMethods().join(","),"fan_get_state");
        fake.clock=16;
        verify(model.panelMethods().indexOf("battery_get_status")>=0);
        verify(first.every(m=>m.indexOf("get_")===0 || m.indexOf("_get_")>=0));
    }
    function test_fanDraftPreservedOnExternalRevisionAndScopeConflict() {
        const curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        const settings={revision:1,global_curve:curve,presets:{},display_unit:"rpm",assign_tdp_presets:true};
        model.snapshots={fan_get_state:{supported:true,settings:settings,limits:{min_rpm:1500,max_rpm:4800},presets:["silent","custom"],readings:{valid:false}}};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);compare(fan.draft.length,8);
        fan.draft=fan.clone(curve);fan.draft[1][1]=1600;fan.dirty=true;
        model.snapshots={fan_get_state:{supported:true,settings:Object.assign({},settings,{revision:2}),limits:{min_rpm:1500,max_rpm:4800},presets:["silent","custom"]}};
        compare(fan.revision,1);compare(fan.draft[1][1],1600);verify(fan.scopes.some(s=>s.id==="custom"));
        fan.dirty=false;fan.load();compare(fan.revision,2);compare(fan.draft[1][1],1500);
    }
    function test_fanAssignmentFailureDoesNotRetargetPresetCurveToGlobal() {
        const globalCurve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        const presetCurve=globalCurve.map(p=>[p[0],Math.min(4800,p[1]+200)]);
        const settings={revision:1,global_curve:globalCurve,presets:{custom:presetCurve},assign_tdp_presets:true};
        model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,settings:settings,presets:["custom"]}});
        model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        fan.switchScope("custom");compare(fan.scope,"custom");
        compare(JSON.stringify(fan.draft),JSON.stringify(presetCurve));
        // Use the same control callback as the real assignment switch.
        function assignmentField(item) {
            if(item.label==="Assign curves to TDP presets")return item;
            for(const child of item.children || []) { const found=assignmentField(child);if(found)return found; }
            return null;
        }
        const assignment=assignmentField(fan);verify(assignment!==null);assignment.edited(false);
        const request=fake.calls.length-1;compare(fake.calls[request].method,"fan_save_assignment_mode");
        response(request,{},"Connection lost. Refresh before retrying a change.");
        compare(fan.scope,"custom","A failed assignment switch must not silently target the global curve");
        compare(JSON.stringify(fan.draft),JSON.stringify(presetCurve));
        compare(JSON.stringify(model.state("fan_get_state").settings.global_curve),JSON.stringify(globalCurve));
    }
    function test_externalAssignmentDisableReconcilesCleanScopeAndPreservesDirtyDraft() {
        const curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        let settings={revision:1,global_curve:curve,presets:{custom:curve},assign_tdp_presets:true};
        function publish(value) { model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,settings:value,presets:["custom"]}}); }
        publish(settings);model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);fan.switchScope("custom");
        publish(Object.assign({},settings,{revision:2,assign_tdp_presets:false}));
        compare(fan.scope,"global");
        publish(Object.assign({},settings,{revision:3}));fan.switchScope("custom");
        const draft=fan.clone(curve);draft[1][1]=1600;fan.draft=draft;fan.dirty=true;
        publish(Object.assign({},settings,{revision:4,assign_tdp_presets:false}));
        compare(fan.scope,"custom");verify(fan.dirty);compare(fan.draft[1][1],1600);
        const count=fake.calls.length;fan.save();
        compare(fake.calls.length,count,"An unavailable TDP scope must not admit a curve save");
        fan.dirty=false;fan.load();compare(fan.scope,"global");
        compare(JSON.stringify(fan.draft),JSON.stringify(curve));
    }
    function test_fanUnitChangeCannotRebaseOverAnExternalCurveRevision() {
        const curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        const initial={revision:1,global_curve:curve,presets:{},display_unit:"rpm",assign_tdp_presets:false};
        function publish(settings) { model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,settings:settings,presets:["custom"]}}); }
        publish(initial);model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        const draft=fan.clone(curve);draft[1][1]=1600;fan.draft=draft;fan.dirty=true;
        const external=Object.assign({},initial,{revision:2,global_curve:curve.map(p=>[p[0],Math.min(4800,p[1]+300)])});
        publish(external);compare(fan.revision,1);
        const units=findChild(fan,"fanUnits");verify(units!==null);
        findChild(units,"option_percent").clicked();const request=fake.calls.length-1;
        compare(fake.calls[request].method,"fan_save_unit");compare(fake.calls[request].args[1],2);
        response(request,{supported:true,settings:Object.assign({},external,{revision:3,display_unit:"percent"}),presets:["custom"]});
        compare(fan.revision,1,"Changing units cannot acknowledge an unseen external curve revision");
        compare(fan.draft[1][1],1600);verify(fan.dirty);
        fan.dirty=false;fan.load();compare(fan.revision,3);
        fan.draft=draft;fan.dirty=true;findChild(units,"option_rpm").clicked();
        response(fake.calls.length-1,{supported:true,settings:Object.assign({},external,{revision:4,display_unit:"rpm"}),presets:["custom"]});
        compare(fan.revision,4,"An own unit-only change can rebase a draft with the same expected revision");
        compare(fan.draft[1][1],1600);verify(fan.dirty);
    }
    function test_combinedControllerBoundsReadsDuringDiagnosticsAndStopsOnLeave() {
        model.page="controller";
        const first=model.panelMethods();
        for(const method of ["vibe_get_settings","rgb_get_status","remap_get_status","controller_get_status"])
            verify(first.indexOf(method)>=0);
        fake.clock=11;compare(model.panelMethods().slice().sort(),["controller_get_status","firmware_get_status"].sort());
        fake.clock=13;verify(model.panelMethods().indexOf("rgb_get_status")>=0);
        model.diagnosticToken="test-token";model.page="battery";
        const stop=fake.calls.find(c=>c.method==="controller_stop_diagnostics");
        verify(stop!==undefined);compare(stop.args[0],"test-token");compare(model.diagnosticToken,"");
        compare(fake.calls[fake.calls.length-1].method,"set_panel_active");
        compare(fake.calls[fake.calls.length-1].args[0],false);
    }
    function test_disabledModulesNeverPolledAndMixedControllerStaysUsable() {
        model.page="controller";
        model.windowActive=true;
        // A clean disable retains ready=true in the real backend.
        model.snapshots={get_version:{blocked:false,modules:{tdp:{ready:true},vibration:{enabled:false,ready:true},rgb:{ready:true},remap:{ready:true},controller:{ready:true},battery:{enabled:false,ready:true}}}};
        model.controllerDetailsAt=-1000;model.refreshPanel();
        verify(!fake.calls.some(c=>c.method.startsWith("vibe_") || c.method.startsWith("battery_")));
        verify(fake.calls.some(c=>c.method==="rgb_get_status"));
        verify(fake.calls.some(c=>c.method==="remap_get_status"));
        model.issue("vibe_get_settings");response(fake.calls.length-1,{},"This module is disabled.");
        compare(model.error,"");compare(model.connected,true);
        model.issue("get_version");response(fake.calls.length-1,model.state("get_version"));
        compare(model.moduleError("vibration"),"");
    }
    function test_gateReadPrecedesGettersAndReconnectNeverReplaysFailedMutation() {
        model.connected=false;model.windowActive=true;
        compare(fake.calls[0].method,"get_version");compare(fake.calls.length,1);
        response(0,{blocked:false,modules:{rgb:{ready:true},vibration:{enabled:false,ready:false},controller:{ready:true}}});
        model.page="controller";wait(20);
        verify(!fake.calls.some(c=>c.method.startsWith("vibe_")));
        model.mutate("rgb_set_control_enabled",[false]);
        const write=fake.calls.findIndex(c=>c.method==="rgb_set_control_enabled");
        response(write,{},"Connection lost. Refresh before retrying a change.");
        compare(fake.calls.filter(c=>c.method==="rgb_set_control_enabled").length,1);
    }
    function test_hiddenTrayChecksContextWithoutHardwarePolling() {
        model.host={installed:true,trayVisible:true};model.refresh();
        const index=fake.calls.length-1;compare(fake.calls[index].method,"get_version");
        response(index,{blocked:false,modules:{fan:{ready:true},tdp:{ready:true}}});
        tryVerify(function(){return fake.calls.some(call=>call.method==="desktop_context");});
        verify(!fake.calls.some(c=>c.method==="fan_get_state" || c.method==="get_tdp_info"));
    }
    function test_invalidModuleGateReadDisconnectsWithoutPublishingOrReplaying() {
        model.issue("get_version");response(0,null);
        verify(!model.connected);
        compare(model.state("get_version").blocked,false);
        verify(model.readErrors.get_version.length>0);
        model.mutate("firmware_check_updates",[]);
        compare(fake.calls.length,1);
        model.issue("get_version");response(1,{blocked:false,modules:{}});
        verify(model.connected);
    }
    function test_invalidDesktopContextCannotKeepPreviousGameOrModeFresh() {
        model.snapshots=Object.assign({},model.snapshots,{desktop_context:{app_id:"42",gamescope:false}});
        for(const invalid of [null,[],false,{app_id:42,gamescope:false},{app_id:"42",gamescope:"false"}]) {
            model.connected=true;
            model.issue("desktop_context");response(fake.calls.length-1,invalid);
            verify(!model.connected);
            compare(model.state("desktop_context").app_id,"42");
            verify(!!model.readErrors.desktop_context);
        }
        model.issue("desktop_context");response(fake.calls.length-1,{app_id:"99",gamescope:true});
        compare(model.state("desktop_context").app_id,"99");
        compare(model.state("desktop_context").gamescope,true);
        compare(model.readErrors.desktop_context,"");
    }
    function test_uninstalledHostRejectsLateRepliesAndAllNewRequests() {
        model.host={installed:true,trayVisible:false};model.issue("get_version");
        model.host={installed:false,trayVisible:false};response(0,{blocked:false,modules:{rgb:{ready:true}}});
        compare(model.connected,false);const count=fake.calls.length;
        model.connected=true;model.mutate("rgb_set_control_enabled",[true]);model.issue("get_version");
        compare(fake.calls.length,count);model.connected=false;
    }
}
