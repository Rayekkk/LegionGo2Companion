// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ColumnLayout {
    required property var host
    UiTheme { id:theme }
    spacing:18
    UiCard {
        Layout.fillWidth:true
        Label { textFormat:Text.PlainText; text:"Desktop integration";color:theme.text;font.pixelSize:17;font.bold:true }
        UiField { Layout.fillWidth:true;label:"Show tray icon";kind:"toggle";value:host.trayEnabled;enabled:host.desktopMode;onEdited:function(value){host.trayEnabled=value;} }
        UiField { Layout.fillWidth:true;label:"Close window to tray";kind:"toggle";value:host.closeToTray;enabled:host.desktopMode && host.trayEnabled && host.trayAvailable;onEdited:function(value){host.closeToTray=value;} }
        Label { textFormat:Text.PlainText; Layout.fillWidth:true;wrapMode:Text.WordWrap;color:theme.muted;font.pixelSize:12;text:host.desktopMode?(host.trayAvailable?"The tray menu can show the window or quit the app.":"No system tray is available in this session. Closing the window quits the app."):"Tray integration is available in Desktop Mode." }
        UiButton { text:"Hide window";enabled:host.trayVisible;onClicked:host.hideWindow() }
    }
}
