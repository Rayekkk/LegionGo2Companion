// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
Switch {
    id:control
    UiTheme { id:theme }
    implicitWidth:60; implicitHeight:44
    padding:3; spacing:0; hoverEnabled:true
    opacity:enabled?1:0.4
    indicator:Rectangle {
        implicitWidth:52; implicitHeight:30; x:control.width-width; y:(control.height-height)/2
        radius:15; color:control.checked?theme.accent:"#3a4655"
        border.color:control.activeFocus?theme.accentText:control.hovered?theme.muted:"transparent"
        border.width:control.activeFocus?2:1
        Rectangle {
            width:24; height:24; radius:12; y:3
            x:control.checked?parent.width-width-3:3
            color:control.checked?"#ffffff":"#c2cdd8"
            Behavior on x { NumberAnimation { duration:110 } }
        }
    }
}
