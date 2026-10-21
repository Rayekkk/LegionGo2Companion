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
        property var cancelled:[]
        property var callsAtCancel:[]
        function cancelReads(inFlight) { cancelled=cancelled.concat([!!inFlight]); callsAtCancel=callsAtCancel.concat([calls.length]); }
        function call(method,args,mutation) {
            const id=++sequence;
            calls=calls.concat([{id:id,method:method,args:args,mutation:mutation}]); return id;
        }
    }
    Component { id:modelFactory; Backend { transport:fake; windowActive:false } }
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
        model.mutate("firmware_check",[]);
        response(0,{phase:"checking"});
        compare(model.state("firmware_get_status").phase,"checking");
        verify(!model.busy);
        model.mutate("firmware_download",[]);
        response(1,{phase:"downloading",component:""});
        compare(model.state("firmware_get_status").component,"");
        compare(model.state("firmware_get_status").phase,"downloading");
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
    function test_whatDoesNotApplyToThisConsoleIsNeverRead() {
        model.page="controller";model.windowActive=true;
        compare(model.panelMethods().indexOf("firmware_get_status")>=0,true);
        model.snapshots={get_version:{blocked:false,modules:{controller:{enabled:true,ready:true,applicable:false}},device:{firmware:false}}};
        verify(!model.canRead("controller_get_status"));
        compare(model.panelMethods().indexOf("firmware_get_status"),-1);
        // Battery applies, the controllers do not (Go S): their levels are not read.
        model.snapshots={get_version:{blocked:false,modules:{battery:{enabled:true,ready:true},controller:{enabled:true,ready:true,applicable:false}}}};
        verify(model.canRead("battery_get_status"));
        verify(!model.canRead("battery_get_controller_levels"));
        model.snapshots={get_version:{blocked:false,modules:{battery:{enabled:true,ready:true},controller:{enabled:false,ready:true}}}};
        verify(model.canRead("battery_get_controller_levels"));  // Turned off, not absent: as before.
        model.snapshots={get_version:{blocked:true,modules:{},device:{firmware:false}}};
        fake.calls=[];model.refreshPanel();
        compare(fake.calls.length,0);
    }
    function test_firmwareMutationFailureRequiresFreshStatusBeforeAnotherAttempt() {
        model.snapshots={firmware_get_status:{phase:"idle"}};
        model.received={firmware_get_status:fake.clock};
        model.mutate("firmware_check",[]);
        response(0,{},"Connection lost; result unknown.");
        verify(model.age("firmware_get_status")>=8);
        verify(!!model.readErrors.firmware_get_status);
        compare(fake.calls.filter(c=>c.method==="firmware_check").length,1);
        model.issue("firmware_get_status");response(1,{phase:"checking"});
        compare(model.age("firmware_get_status"),0);
        compare(model.readErrors.firmware_get_status,"");
    }
    function test_aFollowUpMutationIsSentOnlyAfterItsFirstHalfSucceeds() {
        const then={method:"set_game_ac_profile",args:["99",15000,18000,25000,true,"",true]};
        model.mutate("set_game_profile_active",["99",true],then);model.page="overview";
        response(0,{success:true});
        compare(fake.calls[1].method,"set_game_ac_profile");compare(fake.calls[1].args,then.args,"Leaving the page keeps the target and values");
        response(1,{success:true});fake.calls=[];
        model.mutate("set_game_profile_active",["99",true],then);response(0,{success:false,stderr:"invalid profile request"});
        model.mutate("set_game_profile_active",["99",true],then);response(fake.calls.length-1,{},"Connection lost; result unknown.");
        compare(fake.calls.filter(c=>c.method==="set_game_ac_profile").length,0);
    }
    function test_anRgbWriteUpdatesTheColourTheNextSliderStartsFrom() {
        model.snapshots=Object.assign({},model.snapshots,{rgb_get_status:{settings:{hue:120,saturation:80}}});
        model.mutate("rgb_set_color",[359,80]);
        response(0,{success:true,status:{settings:{hue:359,saturation:80}}});
        compare(model.state("rgb_get_status").settings.hue,359);
        model.mutate("rgb_set_brightness",[50]);response(1,{success:false,error:"busy"});
        compare(model.state("rgb_get_status").settings.hue,359,"A reply without status keeps the last one");
    }
    function test_vibrationChangeWithoutAControllerShowsTheSavedValueAndWhy() {
        const why="Controller not connected. The change is saved and applies when it connects.";
        model.page="controller";
        model.snapshots=Object.assign({},model.snapshots,{get_version:{blocked:false,modules:{vibration:{ready:true}}},
            vibe_get_settings:{settings:{level:1},app_id:"0",profile_id:"0",overwrite:false}});
        model.windowActive=true;model.controllerDetailsAt=fake.clock; // settings were just read
        model.mutate("vibe_set_intensity",[3,"0","0"]);
        // RpcClient reports a failed reply by its error text.
        response(fake.calls.length-1,{success:false,error:why,settings:{level:3},app_id:"0",profile_id:"0",overwrite:false},why);
        compare(model.error,why);
        fake.calls=[];model.refreshPanel();
        const read=fake.calls.findIndex(c=>c.method==="vibe_get_settings");
        verify(read>=0,"The settings are read again right after the change");
        response(read,{settings:{level:3},app_id:"0",profile_id:"0",overwrite:false});
        compare(model.state("vibe_get_settings").settings.level,3);
        compare(model.error,why,"The reason stays shown with the saved value");
    }
    function test_leavingTheEditorPageReadsTheRunningGamesProfileAgain() {
        model.snapshots=Object.assign({},model.snapshots,{desktop_context:{app_id:"42"}});
        model.page="tdp";model.editApp="99";compare(model.profileApp(),"99");
        model.page="overview";model.page="tdp";
        compare([model.editApp,model.profileApp()],["","42"]);
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
    function test_shutdownAbortsTheRunningReadAndReleasesEachLeaseOnce() {
        model.page="controller";model.windowActive=true;model.diagnosticToken="test-token";
        fake.calls=[];fake.cancelled=[];model.shutdown();
        compare(fake.cancelled[0],true);
        compare(fake.calls.filter(call=>call.method==="set_panel_active").length,1);
        verify(fake.calls.every(call=>call.mutation));
        verify(fake.calls.some(call=>call.method==="controller_stop_diagnostics" && call.args[0]==="test-token"));
    }
    function test_shutdownQueuesTheDiagnosticsStopBeforeAbortingReads() {
        model.page="controller";model.windowActive=true;model.diagnosticToken="test-token";
        fake.calls=[];fake.cancelled=[];fake.callsAtCancel=[];model.shutdown();
        compare(fake.calls[0].method,"controller_stop_diagnostics");
        compare(fake.callsAtCancel[0],1);
    }
    function test_captureEndedByTheBackendReleasesTheToken() {
        model.page="controller";model.windowActive=true;model.diagnosticToken="tok";
        fake.calls=[];model.issue("controller_get_diagnostics",["tok"]);
        response(0,{token:"tok",active:false,reason:"completed"});
        compare(model.diagnosticToken,"");
        model.diagnosticToken="tok";
        model.issue("controller_get_diagnostics",["tok"]);
        response(1,undefined,"This controller test is no longer available.");
        compare(model.diagnosticToken,"");
    }
    function test_liveOrStaleDiagnosticsReplyKeepsTheToken() {
        model.page="controller";model.windowActive=true;model.diagnosticToken="tok";
        fake.calls=[];model.issue("controller_get_diagnostics",["tok"]);
        response(0,{token:"tok",active:true});
        compare(model.diagnosticToken,"tok");
        model.issue("controller_get_diagnostics",["old"]);
        response(1,{token:"old",active:false});
        compare(model.diagnosticToken,"tok");
    }
    function test_visibleOverviewRenewsTelemetryLeaseAndHiddenWindowReleasesIt() {
        model.page="overview";model.windowActive=true;
        fake.calls=[];model.refreshPanel();
        verify(fake.calls.some(call=>call.method==="set_panel_active" && call.args[0]===true));
        fake.calls=[];model.windowActive=false;
        verify(fake.calls.some(call=>call.method==="set_panel_active" && call.args[0]===false));
    }
    function test_telemetryLeaseIsRenewedEveryThirtySecondsNotEveryPoll() {
        const leases=()=>fake.calls.filter(c=>c.method==="set_panel_active" && c.args[0]===true).length;
        model.page="overview";model.windowActive=true;
        fake.calls=[];model.refreshPanel();model.refreshPanel();compare(leases(),1);
        fake.clock=39;model.refreshPanel();compare(leases(),1);
        fake.clock=41;model.refreshPanel();compare(leases(),2);
        model.page="battery";model.page="overview";model.refreshPanel();compare(leases(),3);
    }
    function test_lostOrFailedLeaseIsRenewedOnTheNextPollNotAfterThirtySeconds() {
        const leases=()=>fake.calls.filter(c=>c.method==="set_panel_active" && c.args[0]===true);
        model.page="overview";model.windowActive=true;
        fake.calls=[];model.refreshPanel();compare(leases().length,1);
        fake.finished(leases()[0].id,"set_panel_active",{},"Obsolete read."); // navigate() cancelled the queued lease
        fake.clock=12;fake.calls=[];model.refreshPanel();compare(leases().length,1);
        fake.finished(leases()[0].id,"set_panel_active",{},"Connection lost; result unknown.");
        fake.clock=14;fake.calls=[];model.refreshPanel();compare(leases().length,1);
        fake.finished(leases()[0].id,"set_panel_active",true,""); // a granted lease still holds for 30 s
        fake.clock=16;fake.calls=[];model.refreshPanel();compare(leases().length,0);
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
    function test_fanStatusFollowsTheSharedMappingTopDown() {
        const readings={valid:true,automatic:false,rpm:0,apu_temperature:50,age_seconds:0};
        function publish(state) { model.snapshots=Object.assign({},model.snapshots,{fan_get_state:Object.assign({supported:true,readings:readings},state)}); }
        publish({settings:{enabled:false},ownership:{blocked:false,state:"external_override"}});
        model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        const status=findChild(fan,"fanStatus");
        compare(status.text,"Another app controls the fan","a leftover or foreign override while Fan is OFF");
        publish({settings:{enabled:true},telemetry:{active:true,automatic:false},active_curve:"global"});
        compare(status.text,"Global curve");
        publish({settings:{enabled:true},telemetry:{active:true,automatic:false},active_curve:"balanced"});
        compare(status.text,"Balanced TDP curve");
        publish({settings:{enabled:true},telemetry:{active:false,automatic:false}});
        compare(status.text,"Starting your curve…","Fan ON while the guardian re-arms");
        publish({settings:{enabled:true},error:"EC read timed out.",telemetry:{active:false}});
        compare(status.text,"Paused, resuming automatically");
        publish({settings:{enabled:false},error:"EC read timed out."});
        compare(status.text,"Turned off after an error");
        publish({settings:{enabled:false},readings:{valid:true,automatic:true},telemetry:{active:false,automatic:false}});
        compare(status.text,"Returning fan to automatic…");
        publish({settings:{enabled:false},readings:{valid:true,automatic:true},telemetry:{active:false,automatic:true}});
        compare(status.text,"Automatic");
        publish({supported:false,settings:{enabled:false}});
        compare(status.text,"Not available on this console");
    }
    function test_fullscreenEditorButtonsEditTheSelectedPointWithoutAKeyboard() {
        const curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,ownership:{blocked:false},presets:[],limits:{min_rpm:1500,max_rpm:4800},
            settings:{revision:1,enabled:false,global_curve:curve,presets:{},assign_tdp_presets:false,display_unit:"rpm"}}});
        model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        const next=findChild(fan,"editorNext"),warmer=findChild(fan,"editorWarmer"),faster=findChild(fan,"editorFaster"),previous=findChild(fan,"editorPrevious");
        verify(next!==null && warmer!==null && faster!==null && previous!==null);
        for(const button of [next,warmer,faster,previous])verify(button.implicitHeight>=48,"48 px touch target");
        verify(!previous.enabled);next.clicked();verify(previous.enabled);
        warmer.clicked();compare(fan.draft[1][0],46);verify(fan.dirty);
        faster.clicked();compare(fan.draft[1][1],1600);
        findChild(fan,"editorDiscard").clicked();verify(!fan.dirty);compare(fan.draft[1][0],45);
        for(let i=0;i<6;i++)next.clicked();
        verify(!warmer.enabled,"The 100 °C point keeps its temperature");
        const slower=findChild(fan,"editorSlower");verify(slower.enabled && faster.enabled,"The 100 °C point speed is editable");
        slower.clicked();compare(fan.draft[7][0],100);compare(fan.draft[7][1],4700);
    }
    function test_fanCannotBeTurnedOnWhileACurveDraftIsUnsaved() {
        const curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,ownership:{blocked:false},presets:[],
            settings:{revision:1,enabled:false,global_curve:curve,presets:{},assign_tdp_presets:false}}});
        model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        function toggleField(item) {
            if(item.label==="Enable Fan Control")return item;
            for(const child of item.children || []) { const found=toggleField(child);if(found)return found; }
            return null;
        }
        const toggle=toggleField(fan);verify(toggle.enabled);
        fan.dirty=true;verify(!toggle.enabled);compare(toggle.note,"Save or discard your curve changes first.");
        fan.dirty=false;verify(toggle.enabled);
    }
    function test_fanSaveAndUseGlobalFollowTheDeckyRules() {
        const curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,ownership:{blocked:false},presets:["max"],limits:{min_rpm:1500,max_rpm:4800},
            settings:{revision:1,enabled:false,global_curve:curve,presets:{},assign_tdp_presets:true,display_unit:"rpm"}}});
        model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        const save=findChild(fan,"fanSave"),useGlobal=findChild(fan,"fanUseGlobal");
        compare(save.text,"Save global curve");verify(!save.enabled,"nothing to save");
        fan.switchScope("max");compare(save.text,"Assign curve to Maximum");
        verify(save.enabled,"an inheriting preset can take its own copy unchanged");verify(!fan.hasOwnCurve,"no Use global: it already uses the global curve");
        model.snapshots=Object.assign({},model.snapshots,{fan_get_state:Object.assign({},model.snapshots.fan_get_state,{needs_curve_review:true,
            settings:Object.assign({},model.snapshots.fan_get_state.settings,{presets:{max:curve}})})});
        verify(fan.hasOwnCurve && useGlobal.enabled);verify(save.enabled,"an unchanged curve confirms the review");
        fan.dirty=true;verify(!useGlobal.enabled,"unsaved changes first");
    }
    function test_fanBetaNoteOnlyWhereTheBackendCallsFanControlABeta() {
        for(const beta of [undefined,true]) {
            model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,beta:beta,ownership:{blocked:false},presets:[],
                settings:{revision:1,enabled:false,global_curve:[],presets:{}}}});
            model.received={get_version:fake.clock,fan_get_state:fake.clock};
            const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
            compare(fan.betaShown,beta===true);
            const note=findChild(fan,"fanBeta");verify(note!==null);
            if(beta)compare(findChild(fan,"fanBetaText").text,"Beta on this model: the fan is checked by a self-test before Companion first takes control.");
        }
    }
    function test_fanStillStartingHintAlsoForAPageOpenedMidStart() {
        model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,ownership:{blocked:false},presets:[],
            telemetry:{active:false,automatic:false},settings:{revision:1,enabled:true,global_curve:[],presets:{}}}});
        model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        verify(fan.startingSince>=0);compare(fan.status.detail,"No action needed.");
        fan.tick=fan.startingSince+30;compare(fan.status.detail,"Still starting. Turn Fan Control off and on to retry.");
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
        response(request,{},"Connection lost. Check the value, then try again.");
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
    function test_fanOwnDisableKeepsDirtyDraftSaveableButNotOverAForeignCurve() {
        const curve=[[0,1500],[35,1500],[45,1800],[55,2400],[65,3000],[75,3700],[85,4400],[100,4800]];
        const initial={revision:0,enabled:true,global_curve:curve,presets:{},assign_tdp_presets:false,display_unit:"rpm"};
        function publish(settings) { model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,ownership:{blocked:false},presets:[],settings:settings}}); }
        publish(initial);model.received={get_version:fake.clock,fan_get_state:fake.clock};
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        function field(item) {
            if(item.label==="Enable Fan Control")return item;
            for(const child of item.children || []) { const found=field(child);if(found)return found; }
            return null;
        }
        const toggle=field(fan);verify(toggle!==null);
        const draft=fan.clone(curve);draft[1][1]=1600;fan.draft=draft;fan.dirty=true;
        verify(toggle.enabled,"Turning off is never blocked by an unsaved curve");
        toggle.edited(false);let request=fake.calls.length-1;compare(fake.calls[request].method,"fan_set_enabled");
        response(request,{supported:true,ownership:{blocked:false},presets:[],settings:Object.assign({},initial,{enabled:false,revision:1})});
        compare(fan.revision,1,"An own disable keeps the draft saveable");verify(fan.dirty);compare(fan.draft[1][1],1600);
        fan.save();request=fake.calls.length-1;compare(fake.calls[request].method,"fan_save_curve");compare(fake.calls[request].args[2],1);
        response(request,{},"Connection lost.");
        // A foreign curve save lands between the last read and the own toggle: the draft must not rebase over it.
        publish(Object.assign({},initial,{enabled:true,revision:2}));fan.revision=2;
        const foreign=curve.map(p=>[p[0],Math.min(4800,p[1]+300)]);
        toggle.edited(false);request=fake.calls.length-1;compare(fake.calls[request].method,"fan_set_enabled");
        response(request,{supported:true,ownership:{blocked:false},presets:[],settings:Object.assign({},initial,{enabled:false,revision:4,global_curve:foreign})});
        compare(fan.revision,2,"A toggle response that hides another change cannot rebase the draft");
        publish(Object.assign({},initial,{enabled:true,revision:4}));fan.revision=4;
        toggle.edited(false);request=fake.calls.length-1;
        response(request,{supported:true,ownership:{blocked:false},presets:[],settings:Object.assign({},initial,{enabled:false,revision:5,global_curve:foreign})});
        compare(fan.revision,4,"Changed curves in the response are never acknowledged");compare(fan.draft[1][1],1600);verify(fan.dirty);
    }
    function test_fanToggleTurnsOffWhileResumingButBlockedOwnershipPreventsOn() {
        const curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]];
        const blocked={blocked:true,state:"unavailable",reason:"Waiting for fresh controller readings."};
        function publish(enabled) {
            model.snapshots=Object.assign({},model.snapshots,{fan_get_state:{supported:true,ownership:blocked,presets:[],
                settings:{revision:1,enabled:enabled,global_curve:curve,presets:{},assign_tdp_presets:false}}});
            model.received={get_version:fake.clock,fan_get_state:fake.clock};
        }
        function toggleField(item) {
            if(item.label==="Enable Fan Control")return item;
            for(const child of item.children || []) { const found=toggleField(child);if(found)return found; }
            return null;
        }
        publish(true);
        const fan=createTemporaryObject(fanFactory,this,{backend:model});verify(fan!==null);
        const toggle=toggleField(fan);verify(toggle!==null);
        verify(toggle.enabled,"Fan Control can be turned off while it is resuming");
        publish(false);
        verify(!toggle.enabled,"Blocked ownership still prevents turning Fan Control on");
        compare(toggle.note,blocked.reason);
    }
    function test_combinedControllerBoundsReadsDuringDiagnosticsAndStopsOnLeave() {
        model.page="controller";
        const first=model.panelMethods();
        for(const method of ["vibe_get_settings","remap_get_status","controller_get_status"])
            verify(first.indexOf(method)>=0);
        verify(first.indexOf("rgb_get_status")<0,"Lighting has its own page");
        fake.clock=11;compare(model.panelMethods().slice().sort(),["controller_get_status","firmware_get_status"].sort());
        fake.clock=13;verify(model.panelMethods().indexOf("remap_get_status")>=0);
        model.page="rgb";compare(model.panelMethods().join(","),"rgb_get_status");model.page="controller";
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
        response(write,{},"Connection lost. Check the value, then try again.");
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
        model.mutate("firmware_check",[]);
        compare(fake.calls.length,1);
        model.issue("get_version");response(1,{blocked:false,modules:{}});
        verify(model.connected);
    }
    function test_deviceReportRunsBesideTheRestOfTheApp() {
        model.saveDeviceReport();
        compare(fake.calls[0].method,"save_device_report");verify(fake.calls[0].mutation);
        verify(model.reportSaving);verify(!model.busy,"the rest of the app stays usable");
        model.saveDeviceReport();compare(fake.calls.length,1);
        response(0,{success:true,path:"/home/deck/Desktop/report.txt"});
        verify(!model.reportSaving);compare(model.reportText,"Saved to /home/deck/Desktop/report.txt");
        model.saveDeviceReport();response(1,null,"Backend unavailable.");
        verify(!model.reportSaving);compare(model.reportText,"Backend unavailable.");
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
