// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "updates.mjs" as Updates
ColumnLayout {
    id:panel
    property var viewer
    property var backend // save_device_report: the shared backend writes the report, not this app
    property var selectedRecord:null
    property int selectedIndex:-1
    readonly property bool compact:width<760
    readonly property string levelChoice:viewer && viewer.level!=="" && viewer.level!=="all"?viewer.level:"all"
    // Short phrases for the events a user is likely to meet; anything else is shown as its humanised name.
    readonly property var titles:({
        "plugin.startup":"Plugin started","plugin.runtime":"System information","plugin.startup_complete":"Startup finished",
        "plugin.shutdown_started":"Plugin stopping","plugin.shutdown_completed":"Plugin stopped",
        "plugin.rpc_started":"Request started","plugin.rpc_completed":"Request finished","plugin.rpc_failed":"Request failed",
        "plugin.rpc_health":"Backend request check","plugin.module_start_failed":"A module could not start",
        "plugin.uninstall_started":"Uninstall started","plugin.uninstall_completed":"Uninstall finished",
        "diagnostics.file_sink_unavailable":"Event log file unavailable",
        "runtime.asyncio_exception":"Unexpected background error","runtime.background_task_failed":"Background task failed",
        "tdp.limits_applied":"TDP limits applied","tdp.limits_apply_failed":"TDP limits could not be applied",
        "tdp.drift_apply_failed":"TDP correction failed","tdp.unsupported_model":"TDP is not supported on this model",
        "display.mode_changed":"Display mode changed","display.edid_write_rolled_back":"Display change rolled back",
        "display.session_restart_requested":"Display session restart requested",
        "fan.control_fault":"Fan control fault","fan.worker_closed":"Fan control stopped","fan.worker_recovery":"Fan control recovery",
        "fan.worker_start_failed":"Fan control could not start","fan.session_ended":"Fan control session ended",
        "fan.stale_override_released":"Old fan override released","fan.watchdog_start_failed":"Fan watchdog could not start",
        "fan.firmware_validated":"Fan firmware checked",
        "firmware.check_started":"Firmware check started","firmware.check_finished":"Firmware check finished",
        "firmware.download_finished":"Firmware downloaded","firmware.install_started":"Firmware install started",
        "firmware.install_finished":"Firmware install finished","firmware.step_failed":"Firmware step failed",
        "wifi.transaction":"Wi-Fi change","wifi.band_recovery":"Wi-Fi band recovery",
        "wifi.watchdog_recovery_started":"Wi-Fi recovery started","wifi.watchdog_recovery_finished":"Wi-Fi recovery finished",
        "updates.release_check_started":"Update check started","updates.release_check_finished":"Update check finished",
        "updates.release_check_failed":"Update check failed","updates.download_started":"Update download started",
        "updates.download_finished":"Update downloaded","updates.download_failed":"Update download failed",
        "desktop.application_started":"Desktop app started","desktop.application_exiting":"Desktop app closing",
        "desktop.rpc_failed":"Request to the backend failed","desktop.rpc_recovered":"Backend request works again",
        "desktop.backend_session_available":"Connected to the backend","desktop.backend_session_unavailable":"Backend unavailable",
        "desktop.service_start_requested":"Backend start requested","desktop.service_start_completed":"Backend start finished",
        "desktop.tray_visible":"Tray icon shown","desktop.tray_hidden":"Tray icon hidden"
    })
    readonly property var moduleNames:({tdp:"TDP",rgb:"RGB",wifi:"Wi-Fi",edid:"EDID"})
    UiTheme { id:theme }
    spacing:16
    function sourceLabel(value) {
        return ({backend:"Shared backend",desktop:"Desktop app",fan:"Fan worker",wifi:"Wi-Fi worker",firmware:"Firmware worker"})[value] || value;
    }
    function humanize(value) {
        const text=String(value===undefined || value===null?"":value).replace(/_+/g," ").trim();
        return text.charAt(0).toUpperCase()+text.slice(1);
    }
    function moduleLabel(value) { return moduleNames[value] || humanize(value); }
    function eventTitle(row) { return titles[row.component+"."+row.event] || humanize(row.event) || "Event"; }
    // Second line: module, then what was done, how it ended and why, when the record says so.
    function eventDetail(row) {
        const context=row.context || {};
        return [moduleLabel(row.component),context.operation?humanize(context.operation):"",
            context.outcome?"Outcome: "+String(context.outcome).replace(/_+/g," "):"",
            context.reason?"Reason: "+String(context.reason).replace(/_+/g," "):"",
            row.error && row.error.type?"Error: "+row.error.type:""].filter(value=>!!value).join(" · ");
    }
    // Records carry UTC; Date renders them in the user's time zone.
    function timeText(value) { return value?Qt.formatDateTime(new Date(value),"d MMM hh:mm:ss"):"Time unavailable"; }
    function choiceIndex(options,value) { return Math.max(0,options.findIndex(item=>item.id===value)); }
    function clearSelection() { selectedIndex=-1;selectedRecord=null; }
    function warningText(code) {
        const messages={backend_missing:"No backend event files are available yet.",backend_unsafe:"Backend event files could not be read safely.",backend_unavailable:"Backend event files are unavailable.",backend_logs_unavailable:"Backend logs could not be refreshed.",journal_unavailable:"Worker journal is unavailable.",journal_partial:"Only part of the worker journal could be read.",journal_timeout:"Worker journal collection timed out.",journal_output_limit:"Worker journal results were limited.",record_limit_reached:"Showing the newest 1000 collected events. Export uses the visible filters.",desktop_logs_unavailable:"No Desktop event files are available yet.",desktop_log_unreadable:"Desktop logs could not be refreshed.",desktop_log_unsafe:"Desktop logs could not be read safely.",malformed_records:"Some malformed records were skipped.",partial_records:"An incomplete record was skipped."};
        return messages[code] || "Some diagnostic records could not be collected.";
    }
    Component.onCompleted:Qt.callLater(function(){if(viewer)viewer.refresh();})
    onViewerChanged:clearSelection()
    Connections { target:panel.viewer;function onFiltersChanged(){panel.clearSelection();} }
    UiCard {
        Layout.fillWidth:true; padding:18; spacing:12
        GridLayout {
            Layout.fillWidth:true; columns:panel.compact?1:2;columnSpacing:18;rowSpacing:12
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;spacing:4
                Label { text:"Diagnostic events";color:theme.text;font.pixelSize:17;font.weight:Font.DemiBold }
                Label { text:"Recent events from Companion and its background workers. Times are in your local time.";color:theme.muted;font.pixelSize:theme.fontSmall;wrapMode:Text.WordWrap;Layout.fillWidth:true }
            }
            RowLayout {
                Layout.alignment:panel.compact?Qt.AlignLeft:Qt.AlignRight;spacing:10
                BusyIndicator { running:!!panel.viewer && panel.viewer.loading;visible:running;Layout.preferredWidth:26;Layout.preferredHeight:26 }
                UiButton { objectName:"logsRefresh";text:"Refresh logs";enabled:!!panel.viewer && !panel.viewer.loading;onClicked:{panel.clearSelection();panel.viewer.refresh();} }
                UiButton { objectName:"logsCopy";text:"Copy to clipboard";enabled:!!panel.viewer && panel.viewer.exportable && !panel.viewer.loading;onClicked:{searchDelay.stop();panel.viewer.search=search.text;panel.viewer.copyToClipboard();} }
                UiButton { objectName:"logsExport";text:panel.viewer && panel.viewer.exporting?"Exporting…":"Export to Desktop";tone:"primary";enabled:!!panel.viewer && panel.viewer.exportable && !panel.viewer.loading && !panel.viewer.exporting;onClicked:{searchDelay.stop();panel.viewer.search=search.text;panel.viewer.exportFiltered();} }
                UiButton { objectName:"logsDeviceReport";text:panel.backend && panel.backend.reportSaving?"Saving report…":Updates.DEVICE_REPORT_TITLE;enabled:!!panel.backend && panel.backend.connected && !panel.backend.reportSaving;onClicked:panel.backend.saveDeviceReport() }
            }
        }
        Label { objectName:"logsDeviceReportInfo";Layout.fillWidth:true;wrapMode:Text.WordWrap;textFormat:Text.PlainText;color:theme.muted;font.pixelSize:theme.fontSmall;text:Updates.DEVICE_REPORT_DESCRIPTION }
        Label { objectName:"logsDeviceReportResult";Layout.fillWidth:true;wrapMode:Text.WrapAnywhere;textFormat:Text.PlainText;font.pixelSize:theme.fontSmall
            visible:text.length>0;text:panel.backend?panel.backend.reportText:"";color:text.startsWith("Saved to ")?theme.success:theme.warning }
        Label {
            objectName:"logsExportedPath";Layout.fillWidth:true;wrapMode:Text.WrapAnywhere;textFormat:Text.PlainText;color:theme.success;font.pixelSize:theme.fontSmall
            visible:!!panel.viewer && (panel.viewer.exportedPath.length>0 || panel.viewer.copied)
            text:!panel.viewer?"":panel.viewer.copied?"Copied to the clipboard.":"Saved: "+panel.viewer.exportedPath
        }
        ColumnLayout {
            Layout.fillWidth:true;spacing:6
            Label { text:"Show";color:theme.muted;font.pixelSize:theme.fontTiny }
            UiOptions {
                objectName:"logsLevel"
                choices:[{id:"warning",label:"Problems"},{id:"error",label:"Errors only"},{id:"all",label:"All events"}]
                value:panel.levelChoice
                onSelected:function(next){panel.viewer.level=next;}
                Accessible.name:"Log level"
            }
        }
        GridLayout {
            Layout.fillWidth:true; columns:panel.compact?2:3;columnSpacing:14;rowSpacing:12
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;spacing:6
                Label { text:"Source";color:theme.muted;font.pixelSize:theme.fontTiny }
                UiComboBox {
                    objectName:"logsSource";Layout.fillWidth:true;Layout.minimumWidth:0
                    model:[{id:"all",label:"All sources"}].concat(panel.viewer?panel.viewer.sourceOptions.filter(value=>value!=="all" && value!=="").map(value=>({id:value,label:panel.sourceLabel(value)})):[])
                    textRole:"label";valueRole:"id";currentIndex:panel.viewer?panel.choiceIndex(model,panel.viewer.source):0
                    onActivated:panel.viewer.source=currentValue
                    Accessible.name:"Log source"
                }
            }
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;spacing:6
                Label { text:"Time range";color:theme.muted;font.pixelSize:theme.fontTiny }
                UiComboBox {
                    objectName:"logsAge";Layout.fillWidth:true;Layout.minimumWidth:0
                    model:[{id:0,label:"All collected events"},{id:15,label:"Last 15 minutes"},{id:60,label:"Last hour"},{id:1440,label:"Last 24 hours"}];textRole:"label";valueRole:"id"
                    currentIndex:panel.viewer?panel.choiceIndex(model,panel.viewer.ageMinutes):0
                    onActivated:panel.viewer.ageMinutes=currentValue
                    Accessible.name:"Log time range"
                }
            }
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;spacing:6
                Label { text:"Module";color:theme.muted;font.pixelSize:theme.fontTiny }
                UiComboBox {
                    objectName:"logsComponent";Layout.fillWidth:true;Layout.minimumWidth:0
                    model:[{id:"all",label:"All modules"}].concat(panel.viewer?panel.viewer.componentOptions.filter(value=>value!=="all" && value!=="").map(value=>({id:value,label:panel.moduleLabel(value)})):[])
                    textRole:"label";valueRole:"id";currentIndex:panel.viewer?panel.choiceIndex(model,panel.viewer.component):0
                    onActivated:panel.viewer.component=currentValue
                    Accessible.name:"Log module"
                }
            }
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;Layout.columnSpan:panel.compact?2:3;spacing:6
                Label { text:"Search";color:theme.muted;font.pixelSize:theme.fontTiny }
                TextField {
                    id:search;objectName:"logsSearch";Layout.fillWidth:true;implicitHeight:theme.target
                    placeholderText:"Event, operation ID or error code";selectByMouse:true;color:theme.text;font.pixelSize:15
                    text:panel.viewer?panel.viewer.search:""
                    Accessible.name:"Search logs";onTextEdited:searchDelay.restart()
                    background:Rectangle { radius:8;color:theme.input;border.color:search.activeFocus?theme.accentText:theme.border }
                    Timer { id:searchDelay;interval:180;onTriggered:if(panel.viewer)panel.viewer.search=search.text }
                }
            }
        }
        RowLayout {
            Layout.fillWidth:true
            Label {
                objectName:"logsCount";Layout.fillWidth:true;color:theme.muted;font.pixelSize:theme.fontSmall
                text:!panel.viewer?"No events":panel.viewer.level==="warning"
                    ?panel.viewer.filteredCount+(panel.viewer.filteredCount===1?" problem":" problems")+" in "+panel.viewer.totalCount+" collected events"
                    :panel.viewer.filteredCount+" of "+panel.viewer.totalCount+" collected events"
            }
            UiButton { objectName:"logsReset";text:"Reset filters";tone:"quiet";onClicked:{searchDelay.stop();search.text="";panel.viewer.search="";panel.viewer.level="warning";panel.viewer.source="all";panel.viewer.component="all";panel.viewer.ageMinutes=0;} }
        }
    }
    Label {
        objectName:"logsSinkNotice";Layout.fillWidth:true;wrapMode:Text.WordWrap;textFormat:Text.PlainText;color:theme.warning;font.pixelSize:theme.fontSmall
        visible:!!panel.viewer && panel.viewer.fileSinkUnavailable
        text:"The backend could not open its event log file, so only recent events from the system journal are shown."
    }
    Label {
        objectName:"logsStatus";Layout.fillWidth:true;wrapMode:Text.WordWrap;textFormat:Text.PlainText
        visible:text.length>0 && !!panel.viewer && (panel.viewer.stale || panel.viewer.error.length>0);color:theme.warning
        text:panel.viewer?[panel.viewer.error,panel.viewer.status].filter(value=>!!value).join("\n"):""
    }
    Label {
        Layout.fillWidth:true;wrapMode:Text.WordWrap;color:theme.warning;font.pixelSize:theme.fontTiny;textFormat:Text.PlainText
        visible:!!panel.viewer && panel.viewer.warnings.length>0
        text:panel.viewer?panel.viewer.warnings.map(code=>panel.warningText(code)).join(" "):""
    }
    Rectangle {
        Layout.fillWidth:true;Layout.preferredHeight:340;color:theme.card;radius:12;border.color:theme.border;clip:true
        ListView {
            id:events;objectName:"logsList";anchors.fill:parent;anchors.margins:8;clip:true;spacing:4
            model:panel.viewer?panel.viewer.records:[]
            onModelChanged:panel.clearSelection()
            ScrollBar.vertical:ScrollBar {}
            delegate:ItemDelegate {
                id:entry;required property var modelData;required property int index
                width:events.width;height:72
                Accessible.name:entry.modelData.level+" "+panel.eventTitle(entry.modelData)
                onClicked:{panel.selectedIndex=index;panel.selectedRecord=modelData;}
                background:Rectangle { radius:7;color:panel.selectedIndex===entry.index?theme.selected:entry.hovered?theme.hover:"transparent";border.color:entry.activeFocus?theme.accentText:"transparent" }
                contentItem:RowLayout {
                    spacing:12
                    Label { objectName:"logTime";textFormat:Text.PlainText;text:panel.timeText(entry.modelData.time);color:theme.muted;font.pixelSize:theme.fontTiny;Layout.preferredWidth:112 }
                    Label { textFormat:Text.PlainText;text:String(entry.modelData.level || "info").toUpperCase();color:["error","critical"].includes(entry.modelData.level)?theme.danger:entry.modelData.level==="warning"?theme.warning:theme.accentText;font.pixelSize:theme.fontTiny;font.weight:Font.DemiBold;Layout.preferredWidth:65 }
                    ColumnLayout {
                        Layout.fillWidth:true;spacing:4
                        Label { objectName:"logTitle";textFormat:Text.PlainText;text:panel.eventTitle(entry.modelData);color:theme.text;font.pixelSize:theme.fontSmall;elide:Text.ElideRight;Layout.fillWidth:true }
                        Label { objectName:"logDetail";textFormat:Text.PlainText;text:panel.eventDetail(entry.modelData);color:theme.muted;font.pixelSize:theme.fontTiny;elide:Text.ElideRight;Layout.fillWidth:true }
                    }
                }
            }
            Label {
                objectName:"logsEmpty";anchors.centerIn:parent;visible:events.count===0;horizontalAlignment:Text.AlignHCenter;width:parent.width-32;wrapMode:Text.WordWrap
                text:panel.viewer && panel.viewer.loading?"Collecting logs…":panel.viewer && panel.viewer.totalCount>0
                    ?(panel.viewer.level==="warning"?"No warnings or errors. Choose “All events” to see everything.":"No events match these filters.")
                    :"No diagnostic events are available."
                color:theme.muted
            }
        }
    }
    UiCard {
        Layout.fillWidth:true;padding:16;spacing:8
        Label { text:"Event details";color:theme.text;font.pixelSize:theme.fontSmall;font.weight:Font.DemiBold }
        ScrollView {
            Layout.fillWidth:true;Layout.preferredHeight:140;clip:true
            TextArea {
                objectName:"logDetails";readOnly:true;selectByMouse:true;selectByKeyboard:true;textFormat:TextEdit.PlainText
                text:panel.selectedRecord?panel.eventTitle(panel.selectedRecord)+" · "+panel.timeText(panel.selectedRecord.time)+"\n"+JSON.stringify(panel.selectedRecord,null,2):"Select an event to inspect its operation, error and source locations."
                color:panel.selectedRecord?theme.text:theme.muted;font.family:"monospace";font.pixelSize:theme.fontTiny;wrapMode:TextEdit.Wrap
                background:Rectangle { color:"transparent" }
            }
        }
    }
}
