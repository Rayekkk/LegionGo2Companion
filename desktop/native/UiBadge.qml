// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
Rectangle {
    id:badge
    property string text:""
    property string tone:"blue"
    UiTheme { id:theme }
    implicitWidth:label.implicitWidth+20; implicitHeight:28
    radius:7
    color:tone==="neutral"?theme.hover:tone==="green"?"#193c342b":tone==="amber"?"#22c7953e":"#202865ff"
    border.color:tone==="neutral"?theme.border:tone==="green"?"#355fd19b":tone==="amber"?"#35e6ba6b":"#352865ff"
    Label { textFormat:Text.PlainText; id:label; anchors.centerIn:parent; text:badge.text; color:badge.tone==="neutral"?theme.muted:badge.tone==="green"?theme.success:badge.tone==="amber"?theme.warning:theme.accentText; font.pixelSize:13; font.weight:Font.Medium }
}
