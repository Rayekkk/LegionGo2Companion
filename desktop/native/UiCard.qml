// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Layouts
Rectangle {
    id: card
    default property alias content: column.data
    property int padding:22
    property int spacing:14
    UiTheme { id:theme }
    color:theme.card
    radius:14
    border.color:theme.border
    implicitHeight:column.implicitHeight+padding*2
    implicitWidth: 400
    ColumnLayout { id:column; anchors { fill:parent; margins:card.padding } spacing:card.spacing }
}
