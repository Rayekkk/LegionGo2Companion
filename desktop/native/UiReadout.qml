// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
RowLayout {
    id:row
    property string label:""
    property string value:"—"
    UiTheme { id:theme }
    spacing:18
    Label { textFormat:Text.PlainText; text:row.label; color:theme.muted; font.pixelSize:14; Layout.fillWidth:true; Layout.minimumWidth:0; wrapMode:Text.WordWrap }
    Label { textFormat:Text.PlainText; text:row.value; color:theme.text; font.pixelSize:14; font.weight:Font.Medium; Layout.fillWidth:true; Layout.minimumWidth:0; Layout.maximumWidth:300; horizontalAlignment:Text.AlignRight; wrapMode:Text.Wrap }
}
