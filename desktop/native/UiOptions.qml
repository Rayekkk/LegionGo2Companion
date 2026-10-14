// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
Flow {
    id:control
    property var choices:[]
    property var value
    signal selected(var value)
    spacing:7
    Repeater {
        model:control.choices
        delegate:UiButton {
            required property var modelData
            objectName:"option_"+String(modelData.id)
            text:modelData.label;tone:control.value===modelData.id?"primary":"secondary"
            implicitHeight:36;font.pixelSize:12
            Accessible.role:Accessible.RadioButton;Accessible.checked:control.value===modelData.id
            onClicked:if(control.value!==modelData.id)control.selected(modelData.id)
        }
    }
}
