// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
// Every card: optional header (icon, title, badges at the right) over a divider, then its rows.
Rectangle {
    id: card
    default property alias content: column.data
    property alias trailing: trailingRow.data
    property string title:""
    property string icon:""
    property int padding:20
    property int spacing:14
    UiTheme { id:theme }
    color:theme.card
    radius:14
    border.color:theme.border
    implicitHeight:column.implicitHeight+padding*2
    implicitWidth: 400
    ColumnLayout {
        id:column; anchors { fill:parent; margins:card.padding } spacing:card.spacing
        RowLayout {
            id:header; visible:card.title.length>0; Layout.fillWidth:true; Layout.minimumHeight:28; spacing:10
            UiIcon { visible:card.icon.length>0; name:card.icon; color:theme.accentText; Layout.preferredWidth:20; Layout.preferredHeight:20 }
            Label { textFormat:Text.PlainText; text:card.title; color:theme.text; font.pixelSize:18; font.weight:Font.DemiBold; Layout.fillWidth:true; Layout.minimumWidth:0; wrapMode:Text.WordWrap }
            Row { id:trailingRow; spacing:8; Layout.alignment:Qt.AlignVCenter }
        }
        Rectangle { visible:header.visible; Layout.fillWidth:true; implicitHeight:1; color:theme.divider }
    }
}
