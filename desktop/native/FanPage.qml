// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "Fan.js" as Fan
ColumnLayout {
    id:panel
    UiTheme { id:theme }
    required property var backend
    signal navigate(string page)
    ModuleStatus { id:moduleStatus;backend:panel.backend;owner:"fan";tick:panel.tick }
    property var fanState:backend.state("fan_get_state")
    property var settings:fanState.settings || {}
    property var limits:fanState.limits || {}
    property string scope:"global"
    property var draft:[]
    property int revision:-1
    property int unitExpectedRevision:-1
    property int enableExpectedRevision:-1
    property string enableCurves:""
    property bool dirty:false
    readonly property bool editing:dirty
    readonly property bool scopeAvailable:availableScope()
    property int speedStep:100
    property string unit:settings.display_unit || "rpm"
    property real tick:backend.now()
    property var readings:fanState.readings || {}
    property bool fresh:moduleStatus.ready && tick>=0 && Fan.readingFresh(readings,backend.age("fan_get_state"))
    property bool editable:moduleStatus.ready && tick>=0 && backend.age("fan_get_state")<5 && backend.connected && !backend.state("get_version").blocked && !!backend.state("get_version").modules?.fan?.ready && !!fanState.supported && !backend.busy
    readonly property var status:Fan.status(fanState)
    // "Starting your curve…" turns into a retry hint once it lasts 30 s.
    property real startingSince:-1
    onStatusChanged: { if(!status.starting)startingSince=-1;else if(startingSince<0)startingSince=backend.now(); }
    readonly property bool stuck:!!status.starting && startingSince>=0 && tick-startingSince>30
    property var scopes: {
        let values=[{id:"global",label:"Global curve"}];
        if(settings.assign_tdp_presets) for(const name of fanState.presets || []) values.push({id:name,label:name.charAt(0).toUpperCase()+name.slice(1)+" TDP"});
        return values;
    }
    spacing:16
    function clone(points) { return (points || []).map(p=>[p[0],p[1]]); }
    function availableScope() { return scope==="global" || !!settings.assign_tdp_presets && (fanState.presets || []).includes(scope); }
    function load() {
        if (dirty) return;
        if(!availableScope()) scope="global";
        draft=clone(scope==="global"?settings.global_curve:settings.presets?.[scope] || settings.global_curve);
        revision=Number(settings.revision ?? -1);
    }
    function switchScope(value) {
        if(dirty) { pendingScope=value; discardDialog.open(); } else { scope=value; load(); }
    }
    property string pendingScope:"global"
    function save() { if(availableScope()) backend.mutate("fan_save_curve",[scope,clone(draft),revision]); }
    function changeUnit(value) {
        unitExpectedRevision=Number(settings.revision);
        backend.mutate("fan_save_unit",[value,unitExpectedRevision]);
    }
    function curvesOf(value) { return JSON.stringify([value.global_curve,value.presets]); }
    function switchControl(value) {
        enableExpectedRevision=Number(settings.revision);enableCurves=curvesOf(settings);
        backend.mutate("fan_set_enabled",[value]);
    }
    function speed(rpm) { return Fan.speedText(rpm,unit,limits.max_rpm); }
    onUnitChanged: speedStep=unit==="percent"?Math.max(1,Math.round(Number(limits.max_rpm || 4800)/100)):100
    onSettingsChanged: { tick=backend.now(); load(); }
    Component.onCompleted:load()
    Connections {
        target:panel.backend
        function onMutationDone(method,result,success) {
            if(success && method==="fan_save_curve") { panel.dirty=false; panel.load(); }
            if(method==="fan_save_unit") {
                if(success && panel.dirty && panel.revision===panel.unitExpectedRevision) panel.revision=Number(result.settings.revision);
                panel.unitExpectedRevision=-1;
            }
            if(method==="fan_set_enabled") {
                // fan_set_enabled has no expected revision: rebase only when its own +1 is the sole change.
                const next=success?result?.settings:null;
                if(next && panel.dirty && panel.revision===panel.enableExpectedRevision && Number(next.revision)===panel.enableExpectedRevision+1
                        && panel.curvesOf(next)===panel.enableCurves) panel.revision=Number(next.revision);
                panel.enableExpectedRevision=-1;
            }
        }
    }
    Timer { interval:500; repeat:true; running:panel.backend.windowActive; onTriggered:panel.tick=panel.backend.now() }
    UiModuleNotice { Layout.fillWidth:true;status:moduleStatus;onManageRequested:panel.navigate("modules") }
    Label { textFormat:Text.PlainText; text:moduleStatus.ready && typeof backend.moduleError==="function"?backend.moduleError("fan"):"";visible:text.length>0;color:theme.warning;wrapMode:Text.WordWrap;Layout.fillWidth:true }
    UiCard {
        Layout.fillWidth:true
        RowLayout {
            Layout.alignment:Qt.AlignHCenter; spacing:54
            ColumnLayout { Label { textFormat:Text.PlainText; text:"APU temperature"; color:theme.muted; Layout.alignment:Qt.AlignHCenter }
                Label { textFormat:Text.PlainText; text:panel.fresh && tick>=0?Number(readings.apu_temperature).toFixed(1)+"°C":"—"; color:theme.text; font.pixelSize:30; font.weight:Font.DemiBold; Layout.alignment:Qt.AlignHCenter }
                Label { textFormat:Text.PlainText; text:"Temperature: the hotter of CPU and GPU."; color:theme.muted; font.pixelSize:theme.fontTiny; Layout.alignment:Qt.AlignHCenter } }
            ColumnLayout { Label { textFormat:Text.PlainText; text:"Fan speed"; color:theme.muted; Layout.alignment:Qt.AlignHCenter }
                Label { textFormat:Text.PlainText; text:panel.fresh && tick>=0?panel.speed(readings.rpm):"—"; color:theme.text; font.pixelSize:30; font.weight:Font.DemiBold; Layout.alignment:Qt.AlignHCenter } }
        }
        Label { objectName:"fanStatus"; textFormat:Text.PlainText; Layout.alignment:Qt.AlignHCenter; text:moduleStatus.ready && tick>=0 && backend.age("fan_get_state")<5?panel.status.headline:"Reading fan…"; color:theme.text; font.pixelSize:theme.font; font.weight:Font.DemiBold; wrapMode:Text.WordWrap; Layout.fillWidth:true; horizontalAlignment:Text.AlignHCenter }
        UiField {
            Layout.fillWidth:true; label:"Enable Fan Control"; kind:"toggle"; value:settings.enabled
            // Ownership blocks only turning ON; resuming or stale readings must never block turning OFF.
            // Turning on waits for a saved curve; nothing ever blocks turning off.
            enabled:panel.editable && (!!settings.enabled || (!fanState.ownership?.blocked && !panel.dirty && !fanState.needs_curve_review))
            note:!moduleStatus.ready?moduleStatus.message:!settings.enabled && panel.dirty?"Save or discard your curve changes first.":!settings.enabled && fanState.needs_curve_review?"Review and save the curve first.":
                panel.stuck?"Still starting. Turn Fan Control off and on to retry.":panel.status.note
            onEdited: function(value) { panel.switchControl(value); }
        }
    }
    UiCard {
        Layout.fillWidth:true
        GridLayout {
            id:curveToolbar
            Layout.fillWidth:true; columns:panel.width<780?2:3; columnSpacing:12; rowSpacing:12
            ColumnLayout {
                Layout.fillWidth:true; Layout.minimumWidth:0; spacing:6
                Label { textFormat:Text.PlainText; text:"Curve"; color:theme.muted; font.pixelSize:theme.fontSmall }
                UiComboBox { objectName:"fanScope"; Layout.fillWidth:true; Layout.minimumWidth:0; model:panel.scopes; textRole:"label"; valueRole:"id"; currentIndex:panel.scopes.findIndex(s=>s.id===panel.scope); onActivated:panel.switchScope(currentValue); enabled:panel.editable; Accessible.name:"Curve" }
            }
            ColumnLayout {
                Layout.fillWidth:true; Layout.minimumWidth:0; spacing:6
                Label { textFormat:Text.PlainText; text:"Start from:"; color:theme.muted; font.pixelSize:theme.fontSmall }
                Flow {
                    objectName:"fanTemplate"; Layout.fillWidth:true; spacing:8
                    Repeater {
                        model:["silent","balanced","aggressive"]
                        delegate:UiButton {
                            required property string modelData
                            objectName:"template_"+modelData; text:Fan.title(modelData); enabled:panel.editable && !!fanState.templates?.[modelData]
                            onClicked: { panel.draft=panel.clone(fanState.templates[modelData]); panel.dirty=true; }
                        }
                    }
                }
            }
            UiButton { text:"Expand editor"; Layout.alignment:Qt.AlignRight|Qt.AlignBottom; Layout.columnSpan:curveToolbar.columns===2?2:1; onClicked:fullscreen.showFullScreen(); enabled:moduleStatus.ready && panel.draft.length===8 }
        }
        CurveGraph {
            id:graph; Layout.fillWidth:true; Layout.preferredHeight:330
            curve:panel.draft; maximum:Number(limits.max_rpm || 4800); minimumRunning:Number(limits.min_rpm || 1500)
            speedStep:panel.speedStep; unit:panel.unit; writable:panel.editable
            liveValid:panel.fresh && tick>=0; liveTemperature:Number(readings.apu_temperature || 0); liveRpm:Number(readings.rpm || 0)
            onEdited:function(points) { panel.draft=points; panel.dirty=true; }
        }
        RowLayout {
            Layout.fillWidth:true; spacing:16
            Label { textFormat:Text.PlainText; text:"Speed shown as"; color:theme.muted }
            UiOptions { objectName:"fanUnits"; Layout.fillWidth:true; choices:[{id:"rpm",label:"RPM"},{id:"percent",label:"Percent"}]; value:panel.unit; enabled:panel.editable; onSelected:function(value){panel.changeUnit(value);} Accessible.name:"Fan speed units" }
        }
        Label { textFormat:Text.PlainText; visible:panel.dirty && Number(settings.revision)!==panel.revision; text:"Settings changed in Decky or another window. Your draft is preserved; reload before saving."; color:theme.warning; wrapMode:Text.WordWrap; Layout.fillWidth:true }
        Label { textFormat:Text.PlainText; visible:panel.dirty && !panel.scopeAvailable; text:"TDP curve assignments changed. Your draft is preserved. Discard it to return to the global curve."; color:theme.warning; wrapMode:Text.WordWrap; Layout.fillWidth:true }
        Flow {
            Layout.fillWidth:true; spacing:10
            UiButton { text:"Save curve"; tone:"primary"; enabled:panel.editable && panel.scopeAvailable && panel.dirty && Number(settings.revision)===panel.revision; onClicked:panel.save() }
            UiButton { text:"Discard changes"; enabled:panel.dirty && !backend.busy; onClicked: { panel.dirty=false; panel.load(); } }
            UiButton { text:"Use global curve for "+Fan.title(panel.scope); visible:panel.scope!=="global" && panel.scopeAvailable; enabled:panel.editable; onClicked:backend.mutate("fan_save_curve",[panel.scope,null,Number(settings.revision)]) }
            Label { textFormat:Text.PlainText; visible:panel.dirty; text:"Unsaved changes"; color:theme.warning; height:theme.target; verticalAlignment:Text.AlignVCenter; font.pixelSize:theme.fontSmall }
        }
        Label { textFormat:Text.PlainText; text:"0 stops the fan. Running range: "+panel.speed(limits.min_rpm || 1500)+" to "+panel.speed(limits.max_rpm || 4800)+"."; color:theme.muted; font.pixelSize:theme.fontSmall; wrapMode:Text.WordWrap; Layout.fillWidth:true }
    }
    UiCard {
        Layout.fillWidth:true; title:"Settings"; icon:"settings"
        UiField { Layout.fillWidth:true; label:"Assign curves to TDP presets"; kind:"toggle"; value:settings.assign_tdp_presets; enabled:panel.editable && !panel.dirty
            note:"Each Companion TDP preset can have its own curve (not Steam's Performance setting). Others use the global curve."
            onEdited:function(value) { backend.mutate("fan_save_assignment_mode",[value,Number(settings.revision)]); } }
    }
    UiConfirm { id:discardDialog; objectName:"fanDiscardDialog"; title:"Discard unsaved changes?"; text:"Your changes to this curve haven't been applied."; okText:"Discard"
        onAccepted: { panel.dirty=false; panel.scope=panel.pendingScope; panel.load(); } }
    ApplicationWindow {
        objectName:"fanFullscreen"
        id:fullscreen; title:"Fan curve editor"; width:1120; height:780; minimumWidth:800; minimumHeight:600; color:theme.background
        palette { window:theme.background; windowText:theme.text; base:theme.sidebar; text:theme.text;
            button:theme.input; buttonText:theme.text; highlight:theme.accent; highlightedText:"#ffffff";
            light:theme.border; mid:theme.border; dark:theme.sidebar; placeholderText:theme.muted }
        font.pixelSize:theme.font
        modality:Qt.ApplicationModal; transientParent:panel.Window.window
        ColumnLayout {
            anchors { fill:parent; margins:28 } spacing:20
            RowLayout {
                Layout.fillWidth:true
                Label { textFormat:Text.PlainText; text:"Fan curve editor"; color:theme.text; font.pixelSize:24; font.weight:Font.DemiBold; Layout.fillWidth:true; Layout.minimumWidth:0; wrapMode:Text.WordWrap }
                UiComboBox { model:panel.scopes; textRole:"label"; valueRole:"id"; currentIndex:panel.scopes.findIndex(s=>s.id===panel.scope); onActivated:panel.switchScope(currentValue) }
                UiButton { text:"Close"; onClicked:fullscreen.close() }
            }
            CurveGraph {
                id:editorGraph
                Layout.fillWidth:true; Layout.fillHeight:true; curve:panel.draft; maximum:Number(limits.max_rpm || 4800); minimumRunning:Number(limits.min_rpm || 1500)
                unit:panel.unit; speedStep:panel.speedStep; writable:panel.editable
                liveValid:panel.fresh && tick>=0; liveTemperature:Number(readings.apu_temperature || 0); liveRpm:Number(readings.rpm || 0)
                onEdited:function(points) { panel.draft=points; panel.dirty=true; }
            }
            Label { textFormat:Text.PlainText; text:"Drag a point, or select it and use the buttons. The 100°C point can't go below "+panel.speed(limits.min_rpm || 1500)+"."; color:theme.muted; font.pixelSize:theme.fontSmall; Layout.fillWidth:true; wrapMode:Text.WordWrap }
            // Touch first: every edit the keyboard can do has a 48 px button.
            Flow {
                objectName:"fanEditorBar"; Layout.fillWidth:true; spacing:10
                readonly property bool movable:panel.editable && editorGraph.selected<editorGraph.curve.length-1
                UiButton { objectName:"editorPrevious"; text:"◀ Point"; enabled:editorGraph.selected>0; onClicked:editorGraph.selected=Math.max(0,editorGraph.selected-1) }
                UiButton { objectName:"editorNext"; text:"Point ▶"; enabled:editorGraph.selected<editorGraph.curve.length-1; onClicked:editorGraph.selected=Math.min(editorGraph.curve.length-1,editorGraph.selected+1) }
                Item { width:12; height:1 }
                UiButton { objectName:"editorCooler"; text:"− °C"; enabled:parent.movable; onClicked:editorGraph.nudge(-1,0) }
                UiButton { objectName:"editorWarmer"; text:"+ °C"; enabled:parent.movable; onClicked:editorGraph.nudge(1,0) }
                Item { width:12; height:1 }
                UiButton { objectName:"editorSlower"; text:"− speed"; enabled:panel.editable; onClicked:editorGraph.nudge(0,-1) }
                UiButton { objectName:"editorFaster"; text:"+ speed"; enabled:panel.editable; onClicked:editorGraph.nudge(0,1) }
                Item { width:12; height:1 }
                Row { spacing:8 // the step label wraps together with its choice
                Label { textFormat:Text.PlainText; text:"Step:"; color:theme.muted; height:theme.target; verticalAlignment:Text.AlignVCenter }
                UiComboBox {
                    objectName:"editorStep"; Accessible.name:"Speed edit step"
                    width:150
                    model:panel.unit==="percent"?["1%","2%","5%","10%"]:["1 RPM","10 RPM","50 RPM","100 RPM","200 RPM","500 RPM"]
                    currentIndex:panel.unit==="percent"?[1,2,5,10].findIndex(p=>Math.max(1,Math.round(p*Number(limits.max_rpm || 4800)/100))===panel.speedStep):[1,10,50,100,200,500].indexOf(panel.speedStep)
                    onActivated:panel.speedStep=panel.unit==="percent"?Math.max(1,Math.round([1,2,5,10][currentIndex]*Number(limits.max_rpm || 4800)/100)):[1,10,50,100,200,500][currentIndex]
                }
                }
            }
            RowLayout { Layout.fillWidth:true; spacing:10
                Label { textFormat:Text.PlainText; visible:panel.dirty; text:"Unsaved changes"; color:theme.warning; font.pixelSize:theme.fontSmall; Layout.fillWidth:true }
                Item { visible:!panel.dirty; Layout.fillWidth:true }
                UiButton { objectName:"editorDiscard"; text:"Discard"; enabled:panel.dirty && !backend.busy; onClicked: { panel.dirty=false; panel.load(); } }
                UiButton { objectName:"editorSave"; text:"Save curve"; tone:"primary"; enabled:panel.editable && panel.scopeAvailable && panel.dirty && Number(settings.revision)===panel.revision; onClicked:panel.save() }
            }
        }
    }
}
