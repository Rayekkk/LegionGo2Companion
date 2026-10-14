// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
SpinBox {
    id:control
    UiTheme { id:theme }
    implicitHeight:40; implicitWidth:200
    leftPadding:38; rightPadding:38
    editable:true; hoverEnabled:true; opacity:enabled?1:0.42
    contentItem:TextInput {
        text:control.textFromValue(control.value,control.locale)
        font.pixelSize:14; font.weight:Font.DemiBold; color:theme.text
        horizontalAlignment:Text.AlignHCenter; verticalAlignment:Text.AlignVCenter
        readOnly:!control.editable; validator:control.validator
        inputMethodHints:Qt.ImhFormattedNumbersOnly
        selectionColor:theme.accent; selectedTextColor:"white"
    }
    up.indicator:Rectangle {
        x:control.width-width; implicitWidth:36; implicitHeight:40; radius:8
        color:control.up.pressed?theme.accent:control.up.hovered?theme.inputHover:"transparent"
        Text { textFormat:Text.PlainText; anchors.centerIn:parent; text:"+"; color:control.up.enabled?theme.text:theme.faint; font.pixelSize:19 }
    }
    down.indicator:Rectangle {
        implicitWidth:36; implicitHeight:40; radius:8
        color:control.down.pressed?theme.accent:control.down.hovered?theme.inputHover:"transparent"
        Text { textFormat:Text.PlainText; anchors.centerIn:parent; text:"−"; color:control.down.enabled?theme.text:theme.faint; font.pixelSize:19 }
    }
    background:Rectangle { radius:8; color:theme.input; border.color:control.activeFocus?theme.accentText:theme.border; border.width:control.activeFocus?2:1 }
}
