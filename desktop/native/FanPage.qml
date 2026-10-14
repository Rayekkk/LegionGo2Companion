// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
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
    property bool dirty:false
    readonly property bool scopeAvailable:availableScope()
    property int speedStep:100
    property string unit:settings.display_unit || "rpm"
    property real tick:backend.now()
    property var readings:fanState.readings || {}
    property bool fresh:moduleStatus.ready && tick>=0 && !!readings.valid && Number(readings.age_seconds || 0)+backend.age("fan_get_state")<2.5
    property bool editable:moduleStatus.ready && tick>=0 && backend.age("fan_get_state")<5 && backend.connected && !backend.state("get_version").blocked && !!backend.state("get_version").modules?.fan?.ready && !!fanState.supported && !backend.busy
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
    function speed(rpm) { return unit==="percent"?Math.round(100*rpm/Number(limits.max_rpm || 4800))+"%":Math.round(rpm)+" RPM"; }
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
                Label { textFormat:Text.PlainText; text:panel.fresh && tick>=0?Number(readings.apu_temperature).toFixed(1)+"°C":"—"; color:theme.text; font.pixelSize:30; font.weight:Font.DemiBold; Layout.alignment:Qt.AlignHCenter } }
            ColumnLayout { Label { textFormat:Text.PlainText; text:"Fan speed"; color:theme.muted; Layout.alignment:Qt.AlignHCenter }
                Label { textFormat:Text.PlainText; text:panel.fresh && tick>=0?panel.speed(readings.rpm):"—"; color:theme.text; font.pixelSize:30; font.weight:Font.DemiBold; Layout.alignment:Qt.AlignHCenter } }
        }
        Label { textFormat:Text.PlainText; Layout.alignment:Qt.AlignHCenter; text:panel.fresh?(readings.automatic?"Automatic":"Custom curve"):"Sensor readings unavailable"; color:theme.muted; font.pixelSize:12
            HoverHandler { id:sensorHint }
            ToolTip.visible:sensorHint.hovered
            ToolTip.text:"Temperature uses the higher CPU Tctl or GPU edge reading. Fan speed is measured by the console."
        }
        UiField {
            Layout.fillWidth:true; label:"Enable Fan Control"; kind:"toggle"; value:settings.enabled
            enabled:panel.editable && !fanState.ownership?.blocked
            note:!moduleStatus.ready?moduleStatus.message:fanState.ownership?.blocked?fanState.ownership.reason || "Another process controls the fan.":"Turn off to restore automatic fan control."
            onEdited: function(value) { backend.mutate("fan_set_enabled",[value]); }
        }
    }
    UiCard {
        Layout.fillWidth:true
        GridLayout {
            id:curveToolbar
            Layout.fillWidth:true; columns:panel.width<780?2:3; columnSpacing:12; rowSpacing:12
            ColumnLayout {
                Layout.fillWidth:true; Layout.minimumWidth:0; spacing:6
                Label { textFormat:Text.PlainText; text:"Editing curve"; color:theme.muted; font.pixelSize:13 }
                UiComboBox { objectName:"fanScope"; Layout.fillWidth:true; Layout.minimumWidth:0; model:panel.scopes; textRole:"label"; valueRole:"id"; currentIndex:panel.scopes.findIndex(s=>s.id===panel.scope); onActivated:panel.switchScope(currentValue); enabled:panel.editable; Accessible.name:"Editing curve" }
            }
            ColumnLayout {
                Layout.fillWidth:true; Layout.minimumWidth:0; spacing:6
                Label { textFormat:Text.PlainText; text:"Starting template"; color:theme.muted; font.pixelSize:13 }
                UiComboBox {
                    objectName:"fanTemplate"; Layout.fillWidth:true; Layout.minimumWidth:0; model:["Choose a template…","Silent","Balanced","Aggressive"]; currentIndex:0; enabled:panel.editable; Accessible.name:"Starting template"
                    onActivated: { if(currentIndex>0) { panel.draft=panel.clone(fanState.templates?.[["","silent","balanced","aggressive"][currentIndex]]); panel.dirty=true; } currentIndex=0; }
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
            Layout.fillWidth:true
            Label { textFormat:Text.PlainText; text:"Speed edit step"; color:theme.muted }
            UiComboBox {
                Accessible.name:"Speed edit step"
                Layout.preferredWidth:150
                model:panel.unit==="percent"?["1%","2%","5%","10%"]:["1 RPM","10 RPM","50 RPM","100 RPM","200 RPM","500 RPM"]
                currentIndex:panel.unit==="percent"?[1,2,5,10].findIndex(p=>Math.max(1,Math.round(p*Number(limits.max_rpm || 4800)/100))===panel.speedStep):[1,10,50,100,200,500].indexOf(panel.speedStep)
                onActivated:panel.speedStep=panel.unit==="percent"?Math.max(1,Math.round([1,2,5,10][currentIndex]*Number(limits.max_rpm || 4800)/100)):[1,10,50,100,200,500][currentIndex]
            }
            Item { Layout.fillWidth:true }
            UiOptions { objectName:"fanUnits"; Layout.preferredWidth:167; choices:[{id:"rpm",label:"RPM"},{id:"percent",label:"Percent"}]; value:panel.unit; enabled:panel.editable; onSelected:function(value){panel.changeUnit(value);} Accessible.name:"Fan speed units" }
        }
        Label { textFormat:Text.PlainText; visible:panel.dirty && Number(settings.revision)!==panel.revision; text:"Settings changed in Decky or another window. Your draft is preserved; reload before saving."; color:theme.warning; wrapMode:Text.WordWrap; Layout.fillWidth:true }
        Label { textFormat:Text.PlainText; visible:panel.dirty && !panel.scopeAvailable; text:"TDP curve assignments changed. Your draft is preserved. Discard it to return to the global curve."; color:theme.warning; wrapMode:Text.WordWrap; Layout.fillWidth:true }
        Flow {
            Layout.fillWidth:true; spacing:10
            UiButton { text:"Save curve"; tone:"primary"; enabled:panel.editable && panel.scopeAvailable && panel.dirty && Number(settings.revision)===panel.revision; onClicked:panel.save() }
            UiButton { text:"Discard draft"; enabled:panel.dirty && !backend.busy; onClicked: { panel.dirty=false; panel.load(); } }
            UiButton { text:"Use global curve"; visible:panel.scope!=="global" && panel.scopeAvailable; enabled:panel.editable; onClicked:backend.mutate("fan_save_curve",[panel.scope,null,Number(settings.revision)]) }
            Label { textFormat:Text.PlainText; visible:panel.dirty; text:"Unsaved changes"; color:theme.warning; height:40; verticalAlignment:Text.AlignVCenter; font.pixelSize:13 }
        }
        Label { textFormat:Text.PlainText; text:"0 stops the fan. Running range: "+panel.speed(limits.min_rpm || 1500)+" to "+panel.speed(limits.max_rpm || 4800)+"."; color:theme.muted; font.pixelSize:12; wrapMode:Text.WordWrap; Layout.fillWidth:true
            HoverHandler { id:rangeHint }
            ToolTip.visible:rangeHint.hovered
            ToolTip.text:"Limits were verified on this console. The upper limit is not a manufacturer rating."
        }
    }
    UiCard {
        Layout.fillWidth:true
        Label { textFormat:Text.PlainText; text:"Settings"; color:theme.text; font.bold:true; font.pixelSize:17 }
        UiField { Layout.fillWidth:true; label:"Assign curves to TDP presets"; kind:"toggle"; value:settings.assign_tdp_presets; enabled:panel.editable && !panel.dirty
            onEdited:function(value) { backend.mutate("fan_save_assignment_mode",[value,Number(settings.revision)]); } }
        Label { textFormat:Text.PlainText; text:"Assignments apply to Companion TDP presets, not SteamOS QAM Performance modes. Unassigned presets use the global curve."; color:theme.warning; font.pixelSize:12; wrapMode:Text.WordWrap; Layout.fillWidth:true }
    }
    Dialog { id:discardDialog; title:"Discard this unsaved draft?"; modal:true; standardButtons:Dialog.Ok|Dialog.Cancel
        onAccepted: { panel.dirty=false; panel.scope=panel.pendingScope; panel.load(); } }
    ApplicationWindow {
        objectName:"fanFullscreen"
        id:fullscreen; title:"Fan curve editor"; width:1120; height:780; minimumWidth:800; minimumHeight:600; color:theme.background
        palette { window:theme.background; windowText:theme.text; base:theme.sidebar; text:theme.text;
            button:theme.input; buttonText:theme.text; highlight:theme.accent; highlightedText:"#ffffff";
            light:theme.border; mid:theme.border; dark:theme.sidebar; placeholderText:theme.muted }
        font.pixelSize:14
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
                Layout.fillWidth:true; Layout.fillHeight:true; curve:panel.draft; maximum:Number(limits.max_rpm || 4800); minimumRunning:Number(limits.min_rpm || 1500)
                unit:panel.unit; speedStep:panel.speedStep; writable:panel.editable
                liveValid:panel.fresh && tick>=0; liveTemperature:Number(readings.apu_temperature || 0); liveRpm:Number(readings.rpm || 0)
                onEdited:function(points) { panel.draft=points; panel.dirty=true; }
            }
            RowLayout { Layout.fillWidth:true
                Label { textFormat:Text.PlainText; text:"Enter: edit · Q/E: previous/next point · Esc: finish editing"; color:theme.muted; Layout.fillWidth:true; Layout.minimumWidth:0; wrapMode:Text.WordWrap; font.pixelSize:13 }
                UiButton { text:"Save curve"; tone:"primary"; enabled:panel.editable && panel.scopeAvailable && panel.dirty && Number(settings.revision)===panel.revision; onClicked:panel.save() }
            }
        }
    }
}
