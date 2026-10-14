// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ColumnLayout {
    id:panel
    property var viewer
    property var selectedRecord:null
    property int selectedIndex:-1
    readonly property bool compact:width<760
    UiTheme { id:theme }
    spacing:16
    function sourceLabel(value) {
        return ({backend:"Shared backend",desktop:"Desktop app",fan:"Fan worker",wifi:"Wi-Fi worker",firmware:"Firmware worker"})[value] || value;
    }
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
                Label { text:"Recent events from Companion and its background workers.";color:theme.muted;font.pixelSize:12;wrapMode:Text.WordWrap;Layout.fillWidth:true }
            }
            RowLayout {
                Layout.alignment:panel.compact?Qt.AlignLeft:Qt.AlignRight;spacing:10
                BusyIndicator { running:!!panel.viewer && panel.viewer.loading;visible:running;Layout.preferredWidth:26;Layout.preferredHeight:26 }
                UiButton { objectName:"logsRefresh";text:"Refresh logs";enabled:!!panel.viewer && !panel.viewer.loading;onClicked:{panel.clearSelection();panel.viewer.refresh();} }
                UiButton { objectName:"logsExport";text:panel.viewer && panel.viewer.exporting?"Exporting…":"Export to Desktop";tone:"primary";enabled:!!panel.viewer && panel.viewer.filteredCount>0 && !panel.viewer.loading && !panel.viewer.exporting;onClicked:{searchDelay.stop();panel.viewer.search=search.text;panel.viewer.exportFiltered();} }
            }
        }
        GridLayout {
            Layout.fillWidth:true; columns:panel.compact?2:3;columnSpacing:14;rowSpacing:12
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;spacing:6
                Label { text:"Level";color:theme.muted;font.pixelSize:12 }
                UiComboBox {
                    objectName:"logsLevel";Layout.fillWidth:true;Layout.minimumWidth:0
                    model:[{id:"all",label:"All levels"},{id:"warning",label:"Warnings and errors"},{id:"error",label:"Errors only"}];textRole:"label";valueRole:"id"
                    currentIndex:panel.viewer?panel.choiceIndex(model,panel.viewer.level):0
                    onActivated:panel.viewer.level=currentValue
                    Accessible.name:"Log level"
                }
            }
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;spacing:6
                Label { text:"Source";color:theme.muted;font.pixelSize:12 }
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
                Label { text:"Time range";color:theme.muted;font.pixelSize:12 }
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
                Label { text:"Module";color:theme.muted;font.pixelSize:12 }
                UiComboBox {
                    objectName:"logsComponent";Layout.fillWidth:true;Layout.minimumWidth:0
                    model:[{id:"all",label:"All modules"}].concat(panel.viewer?panel.viewer.componentOptions.filter(value=>value!=="all" && value!=="").map(value=>({id:value,label:value})):[])
                    textRole:"label";valueRole:"id";currentIndex:panel.viewer?panel.choiceIndex(model,panel.viewer.component):0
                    onActivated:panel.viewer.component=currentValue
                    Accessible.name:"Log module"
                }
            }
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;Layout.columnSpan:2;spacing:6
                Label { text:"Search";color:theme.muted;font.pixelSize:12 }
                TextField {
                    id:search;objectName:"logsSearch";Layout.fillWidth:true;implicitHeight:40
                    placeholderText:"Event, operation ID or error code";selectByMouse:true;color:theme.text
                    text:panel.viewer?panel.viewer.search:""
                    Accessible.name:"Search logs";onTextEdited:searchDelay.restart()
                    background:Rectangle { radius:8;color:theme.input;border.color:search.activeFocus?theme.accentText:theme.border }
                    Timer { id:searchDelay;interval:180;onTriggered:if(panel.viewer)panel.viewer.search=search.text }
                }
            }
        }
        RowLayout {
            Layout.fillWidth:true
            Label { objectName:"logsCount";text:panel.viewer?panel.viewer.filteredCount+" of "+panel.viewer.totalCount+" collected events":"No events";color:theme.muted;font.pixelSize:12;Layout.fillWidth:true }
            UiButton { text:"Reset filters";tone:"quiet";onClicked:{searchDelay.stop();search.text="";panel.viewer.search="";panel.viewer.level="all";panel.viewer.source="all";panel.viewer.component="all";panel.viewer.ageMinutes=0;} }
        }
    }
    Label {
        objectName:"logsStatus";Layout.fillWidth:true;wrapMode:Text.WordWrap;textFormat:Text.PlainText
        visible:text.length>0 && !!panel.viewer && (panel.viewer.stale || panel.viewer.error.length>0);color:theme.warning
        text:panel.viewer?[panel.viewer.error,panel.viewer.status].filter(value=>!!value).join("\n"):""
    }
    Label {
        Layout.fillWidth:true;wrapMode:Text.WordWrap;color:theme.warning;font.pixelSize:12;textFormat:Text.PlainText
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
                width:events.width;height:68
                Accessible.name:entry.modelData.level+" "+entry.modelData.component+" "+entry.modelData.event
                onClicked:{panel.selectedIndex=index;panel.selectedRecord=modelData;}
                background:Rectangle { radius:7;color:panel.selectedIndex===entry.index?theme.selected:entry.hovered?theme.hover:"transparent";border.color:entry.activeFocus?theme.accentText:"transparent" }
                contentItem:RowLayout {
                    spacing:12
                    Label { textFormat:Text.PlainText;text:entry.modelData.time?Qt.formatDateTime(new Date(entry.modelData.time),"MM-dd hh:mm:ss"):"Time unavailable";color:theme.muted;font.pixelSize:11;Layout.preferredWidth:105 }
                    Label { textFormat:Text.PlainText;text:String(entry.modelData.level || "info").toUpperCase();color:["error","critical"].includes(entry.modelData.level)?theme.danger:entry.modelData.level==="warning"?theme.warning:theme.accentText;font.pixelSize:10;font.weight:Font.DemiBold;Layout.preferredWidth:65 }
                    ColumnLayout {
                        Layout.fillWidth:true;spacing:5
                        Label { textFormat:Text.PlainText;text:entry.modelData.event || "Event";color:theme.text;font.pixelSize:13;elide:Text.ElideRight;Layout.fillWidth:true }
                        Label { textFormat:Text.PlainText;text:[entry.modelData.component,panel.sourceLabel(entry.modelData.source),entry.modelData.context?.stage,entry.modelData.error?.errno!==undefined?"errno "+entry.modelData.error.errno:""].filter(value=>!!value).join(" · ");color:theme.muted;font.pixelSize:11;elide:Text.ElideRight;Layout.fillWidth:true }
                    }
                }
            }
            Label {
                objectName:"logsEmpty";anchors.centerIn:parent;visible:events.count===0
                text:panel.viewer && panel.viewer.loading?"Collecting logs…":panel.viewer && panel.viewer.totalCount>0?"No events match these filters.":"No diagnostic events are available."
                color:theme.muted
            }
        }
    }
    UiCard {
        Layout.fillWidth:true;padding:16;spacing:8
        Label { text:"Event details";color:theme.text;font.pixelSize:14;font.weight:Font.DemiBold }
        ScrollView {
            Layout.fillWidth:true;Layout.preferredHeight:140;clip:true
            TextArea {
                objectName:"logDetails";readOnly:true;selectByMouse:true;selectByKeyboard:true;textFormat:TextEdit.PlainText
                text:panel.selectedRecord?JSON.stringify(panel.selectedRecord,null,2):"Select an event to inspect its operation, error and source locations."
                color:panel.selectedRecord?theme.text:theme.muted;font.family:"monospace";font.pixelSize:12;wrapMode:TextEdit.Wrap
                background:Rectangle { color:"transparent" }
            }
        }
    }
    Label {
        objectName:"logsExportedPath";Layout.fillWidth:true;wrapMode:Text.WrapAnywhere;textFormat:Text.PlainText;color:theme.success;font.pixelSize:12
        visible:!!panel.viewer && panel.viewer.exportedPath.length>0
        text:panel.viewer && panel.viewer.exportedPath?"Saved: "+panel.viewer.exportedPath:""
    }
}
