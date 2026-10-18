// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
// The "Desktop app" and "Diagnostics" cards of the About page.
ColumnLayout {
    required property var host
    signal viewLogs()
    UiTheme { id:theme }
    spacing:theme.gap
    UiCard {
        Layout.fillWidth:true; title:"Desktop app"; icon:"settings"
        UiField { Layout.fillWidth:true;label:"Show tray icon";kind:"toggle";value:host.trayEnabled;enabled:host.desktopMode;onEdited:function(value){host.trayEnabled=value;} }
        UiField { Layout.fillWidth:true;label:"Close window to tray";kind:"toggle";value:host.closeToTray;enabled:host.desktopMode && host.trayEnabled && host.trayAvailable;onEdited:function(value){host.closeToTray=value;} }
        Label { textFormat:Text.PlainText; Layout.fillWidth:true;wrapMode:Text.WordWrap;color:theme.muted;font.pixelSize:theme.fontSmall;text:host.desktopMode?(host.trayAvailable?"Companion starts in the tray when Desktop Mode starts. The tray menu can show the window or quit the app.":"No system tray is available in this session. Closing the window quits the app."):"Tray integration is available in Desktop Mode." }
    }
    UiCard {
        Layout.fillWidth:true; title:"Diagnostics"; icon:"logs"
        Label { textFormat:Text.PlainText; Layout.fillWidth:true;wrapMode:Text.WordWrap;color:theme.muted;font.pixelSize:theme.fontSmall;text:"Recent events from Companion and its background workers." }
        UiButton { objectName:"viewLogs";text:"View logs";onClicked:viewLogs() }
    }
}
