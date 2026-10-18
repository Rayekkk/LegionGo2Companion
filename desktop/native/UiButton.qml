// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
Button {
    id:control
    // primary: the one commit action of a card; secondary: every other action; danger: destructive.
    property string tone:"secondary"
    UiTheme { id:theme }
    implicitHeight:theme.target
    implicitWidth:Math.max(88,label.implicitWidth+34)
    horizontalPadding:17; verticalPadding:10
    hoverEnabled:true
    opacity:enabled?1:0.42
    font.pixelSize:15; font.weight:Font.DemiBold
    contentItem:Text { textFormat:Text.PlainText;
        id:label; text:control.text; font:control.font
        color:control.tone==="primary"?"#ffffff":control.tone==="danger"?theme.danger:theme.text
        horizontalAlignment:Text.AlignHCenter; verticalAlignment:Text.AlignVCenter
        elide:Text.ElideRight
    }
    background:Rectangle {
        radius:8
        color:control.tone==="primary"?(control.down?"#2054da":control.hovered?theme.accentHover:theme.accent):control.down?theme.inputHover:control.hovered?theme.hover:theme.input
        border.color:control.activeFocus?theme.accentText:control.tone==="primary"?"transparent":theme.border
        border.width:control.activeFocus?2:1
    }
    Accessible.name:text
}
