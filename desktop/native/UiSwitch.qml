// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
Switch {
    id:control
    UiTheme { id:theme }
    implicitWidth:52; implicitHeight:34
    padding:3; spacing:0; hoverEnabled:true
    opacity:enabled?1:0.4
    indicator:Rectangle {
        implicitWidth:42; implicitHeight:24; x:control.leftPadding; y:(control.height-height)/2
        radius:12; color:control.checked?theme.accent:"#3a4655"
        border.color:control.activeFocus?theme.accentText:control.hovered?theme.muted:"transparent"
        border.width:control.activeFocus?2:1
        Rectangle {
            width:18; height:18; radius:9; y:3
            x:control.checked?parent.width-width-3:3
            color:control.checked?"#ffffff":"#c2cdd8"
            Behavior on x { NumberAnimation { duration:110 } }
        }
    }
}
