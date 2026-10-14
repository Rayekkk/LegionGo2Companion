// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ColumnLayout {
    id:metric
    property string label:""
    property string value:"—"
    property string note:""
    property string help:""
    UiTheme { id:theme }
    spacing:7
    HoverHandler { id:hover }
    ToolTip.visible:hover.hovered && metric.help.length>0
    ToolTip.text:metric.help
    Label { textFormat:Text.PlainText; text:metric.label; color:theme.muted; font.pixelSize:13; Layout.fillWidth:true; elide:Text.ElideRight }
    Label { textFormat:Text.PlainText; text:metric.value; color:theme.text; font.pixelSize:25; font.weight:Font.DemiBold; Layout.fillWidth:true; elide:Text.ElideRight }
    Label { textFormat:Text.PlainText; text:metric.note; visible:text.length>0; color:theme.faint; font.pixelSize:11; Layout.fillWidth:true; elide:Text.ElideRight }
}
