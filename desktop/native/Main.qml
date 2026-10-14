// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ApplicationWindow {
    id:window
    objectName:"companionWindow"
    width:1340; height:940; minimumWidth:980; minimumHeight:680
    visible:true; title:"Legion Go Companion"
    UiTheme { id:theme }
    color:theme.background
    property string page:"overview"
    property var pages:[
        {id:"overview",label:"Overview"},{id:"tdp",label:"TDP"},{id:"fan",label:"Fan Control"},
        {id:"controller",label:"Controller"},{id:"battery",label:"Battery"},{id:"display",label:"Display"},
        {id:"wifi",label:"Wi-Fi"},{id:"settings",label:"App settings"},{id:"modules",label:"Modules"},{id:"logs",label:"Logs"},{id:"about",label:"About & updates"}]
    property bool closing:false
    property bool explicitQuit:false
    onPageChanged:Qt.callLater(function(){scroll.contentItem.contentY=0;})
    function quitApp() { explicitQuit=true;window.close(); }
    Connections { target:appHost;function onQuitRequested(){window.quitApp();} }
    Connections { target:bridgeModel;function onChanged(method){if(method==="desktop_context")appHost.setContext(bridgeModel.connected && bridgeModel.age(method)<8,!!bridgeModel.state(method).gamescope);} }
    Connections { target:appHost;function onChanged(){if(!appHost.installed){bridgeModel.connected=false;rpc.cancelReads();}} }
    property alias backendProblem:bridgeModel.error
    palette { window:theme.background; windowText:theme.text; base:theme.input; text:theme.text;
        button:theme.input; buttonText:theme.text; highlight:theme.accent; highlightedText:"#ffffff";
        light:theme.border; mid:theme.border; dark:theme.cardBottom; placeholderText:theme.muted }
    font.pixelSize:14
    Backend { id:bridgeModel; transport:rpc;host:appHost; page:window.page; windowActive:window.visible && window.visibility!==Window.Minimized && !window.closing }
    onClosing:function(event) {
        if(!explicitQuit && appHost.closeToTray && appHost.hideWindow()){event.accepted=false;return;}
        if(!closing) { event.accepted=false; closing=true; bridgeModel.shutdown(); exitTimer.start(); }
    }
    Timer { id:exitTimer; interval:1500; onTriggered:Qt.quit() }
    RowLayout {
        anchors.fill:parent; spacing:0
        Rectangle {
            Layout.preferredWidth:214; Layout.fillHeight:true; color:theme.sidebar
            Rectangle { anchors.right:parent.right; width:1; height:parent.height; color:theme.divider }
            ColumnLayout {
                anchors { fill:parent; leftMargin:18; rightMargin:18; topMargin:window.height<760?20:28; bottomMargin:22 } spacing:8
                RowLayout { spacing:12; Layout.leftMargin:9; Layout.bottomMargin:24
                    Rectangle { width:38; height:38; radius:12; color:"#24344d"; border.color:"#354f7b"
                        Label { textFormat:Text.PlainText; anchors.centerIn:parent; text:"L2"; color:theme.accentText; font.pixelSize:16; font.weight:Font.DemiBold }
                    }
                    ColumnLayout { spacing:2
                        Label { textFormat:Text.PlainText; text:"LEGION GO"; font.weight:Font.DemiBold; font.pixelSize:15; font.letterSpacing:0.6; color:theme.text }
                        Label { textFormat:Text.PlainText; text:"Companion"; color:theme.muted; font.pixelSize:12 }
                    }
                }
                ScrollView {
                    id:sidebarScroll
                    Layout.fillWidth:true; Layout.fillHeight:true; Layout.minimumHeight:0; clip:true
                    contentWidth:availableWidth
                    ScrollBar.horizontal.policy:ScrollBar.AlwaysOff
                    ColumnLayout {
                        width:sidebarScroll.availableWidth; spacing:5
                        Repeater {
                            model:window.pages
                            delegate:UiButton {
                                id:nav
                                required property var modelData
                                Layout.fillWidth:true; Layout.preferredHeight:window.height<760?38:43
                                Layout.topMargin:modelData.id==="settings"?(window.height<760?12:20):0
                                checked:window.page===modelData.id; checkable:true
                                Accessible.name:modelData.label
                                onClicked:window.page=modelData.id
                                onActiveFocusChanged: {
                                    if(!activeFocus)return;
                                    const viewport=sidebarScroll.contentItem;
                                    if(y<viewport.contentY)viewport.contentY=y;
                                    else if(y+height>viewport.contentY+viewport.height)viewport.contentY=y+height-viewport.height;
                                }
                                contentItem:RowLayout { spacing:12
                                    UiIcon { name:nav.modelData.id; color:nav.checked?theme.accentText:theme.muted; Layout.preferredWidth:19; Layout.preferredHeight:19 }
                                    Label { textFormat:Text.PlainText; text:nav.modelData.label; color:nav.checked?theme.text:theme.muted; font.pixelSize:14; font.weight:nav.checked?Font.DemiBold:Font.Normal; Layout.fillWidth:true }
                                }
                                background:Rectangle { radius:9; color:nav.checked?theme.selected:nav.hovered?theme.hover:"transparent"; border.color:nav.activeFocus?theme.accentText:"transparent"; border.width:nav.activeFocus?2:1 }
                            }
                        }
                    }
                }
                Rectangle { Layout.fillWidth:true; implicitHeight:1; color:theme.divider; Layout.bottomMargin:10 }
                RowLayout { Layout.leftMargin:9; spacing:8
                    Rectangle { width:6; height:6; radius:3; color:bridgeModel.connected?theme.success:theme.warning }
                    Label { textFormat:Text.PlainText; text:!appHost.installed?"Plugin removed":bridgeModel.connected?"Connected to Decky":"Decky unavailable"; color:theme.muted; font.pixelSize:11 }
                }
            }
        }
        ColumnLayout {
            Layout.fillWidth:true; Layout.fillHeight:true; Layout.margins:window.width<1100?20:28; spacing:22
            RowLayout {
                Layout.fillWidth:true
                Label { textFormat:Text.PlainText; text:window.pages.find(p=>p.id===window.page)?.label || "Companion"; color:theme.text; font.pixelSize:28; font.weight:Font.DemiBold; Layout.minimumWidth:0; elide:Text.ElideRight }
                Item { Layout.fillWidth:true }
                UiBadge { visible:bridgeModel.connected && bridgeModel.age("desktop_context")<8; text:bridgeModel.state("desktop_context").gamescope?"Gaming Mode":"Desktop Mode" }
                BusyIndicator { running:bridgeModel.busy; visible:running; Layout.preferredWidth:26; Layout.preferredHeight:26 }
                UiButton { text:"Refresh"; visible:window.page!=="logs";enabled:!bridgeModel.busy; onClicked:bridgeModel.refreshNow() }
            }
            UiCard {
                visible:!bridgeModel.connected || !appHost.installed;Layout.fillWidth:true;padding:18;spacing:10
                Label { textFormat:Text.PlainText; text:!appHost.installed?"Companion has been uninstalled":"Companion is disconnected";color:theme.text;font.pixelSize:17;font.bold:true }
                Label { textFormat:Text.PlainText; Layout.fillWidth:true;wrapMode:Text.WordWrap;color:theme.muted;text:!appHost.installed?"The desktop app can no longer control the device. Reinstall Companion in Decky to use it again.":appHost.serviceState==="active"?"Decky is running, but Companion is not responding. Check whether the plugin is enabled. Saved drafts remain in this window.":"Controls are paused. Reconnect when Decky and Companion are running. Saved drafts remain in this window." }
                RowLayout {
                    UiButton { text:"Reconnect";visible:appHost.installed;enabled:!bridgeModel.busy;onClicked:{bridgeModel.refreshNow();appHost.inspectService();} }
                    UiButton { text:"Check Decky";visible:appHost.installed;enabled:!appHost.serviceBusy;onClicked:appHost.inspectService() }
                    UiButton { text:"Start Decky";visible:appHost.canStartService;onClicked:startDecky.open() }
                    UiButton { text:"Quit app";onClicked:window.quitApp() }
                }
                Label { textFormat:Text.PlainText; text:appHost.serviceError;visible:text.length>0;color:theme.warning;wrapMode:Text.WordWrap;Layout.fillWidth:true }
            }
            Rectangle {
                visible:bridgeModel.error.length>0 || !!bridgeModel.state("get_version").blocked
                Layout.fillWidth:true; implicitHeight:message.implicitHeight+28; color:"#253c2d20"; border.color:"#55e6ba6b"; radius:12
                Label { textFormat:Text.PlainText; id:message; anchors { fill:parent; margins:14 } wrapMode:Text.WordWrap; color:theme.warning; text:bridgeModel.error || bridgeModel.state("get_version").message || "Controls are unavailable. Check the enabled modules and conflicting plugins." }
            }
            ScrollView {
                id:scroll
                Layout.fillWidth:true; Layout.fillHeight:true; clip:true
                contentWidth:availableWidth; ScrollBar.horizontal.policy:ScrollBar.AlwaysOff
                Loader { width:scroll.availableWidth; sourceComponent:window.page==="overview"?dashboard:window.page==="fan"?fan:window.page==="settings"?preferences:window.page==="logs"?logBrowser:module }
            }
        }
    }
    Component { id:dashboard; OverviewPage { backend:bridgeModel; onNavigate:function(page) { window.page=page; } } }
    Component { id:fan; FanPage { backend:bridgeModel;onNavigate:function(page){window.page=page;} } }
    Component { id:module; ModulePage { backend:bridgeModel; page:window.page;onNavigate:function(page){window.page=page;} } }
    Component { id:preferences; AppSettings { host:appHost } }
    Component { id:logBrowser; LogsPage { viewer:logViewer } }
    Connections { target:bridgeModel;function onConnectedChanged(){if(!bridgeModel.connected)appHost.inspectService();} }
    Dialog { id:startDecky;title:"Start Decky?";modal:true;standardButtons:Dialog.Ok|Dialog.Cancel
        anchors.centerIn:Overlay.overlay
        Label { textFormat:Text.PlainText; text:"SteamOS may ask for authorization. This starts the stopped Decky service; settings are preserved.";width:420;wrapMode:Text.WordWrap;color:theme.text }
        onAccepted:appHost.startService()
    }
}
