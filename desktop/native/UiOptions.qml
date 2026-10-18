// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
Flow {
    id:control
    property var choices:[]
    property var value
    // Choices with a `detail` text stack one per row, the text visible under the button.
    readonly property bool detailed:(choices || []).some(c=>!!c.detail)
    signal selected(var value)
    spacing:detailed?12:7
    UiTheme { id:theme }
    Repeater {
        model:control.choices
        delegate:ColumnLayout {
            id:choice
            required property var modelData
            width:control.detailed?control.width:implicitWidth
            spacing:4
            UiButton {
                objectName:"option_"+String(choice.modelData.id)
                text:choice.modelData.label;tone:control.value===choice.modelData.id?"primary":"secondary"
                Accessible.role:Accessible.RadioButton;Accessible.checked:control.value===choice.modelData.id
                Accessible.description:choice.modelData.detail || ""
                onClicked:if(control.value!==choice.modelData.id)control.selected(choice.modelData.id)
            }
            Label {
                textFormat:Text.PlainText;visible:!!choice.modelData.detail;text:choice.modelData.detail || ""
                color:theme.muted;font.pixelSize:theme.fontSmall;wrapMode:Text.WordWrap;Layout.fillWidth:true
            }
        }
    }
}
