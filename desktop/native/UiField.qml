// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
ColumnLayout {
    id: field
    UiTheme { id:theme }
    property string label: ""
    property string kind: "text"
    property var value
    property var choices: []
    property int minimum: 0
    property int maximum: 100
    property int step: 1
    property string note: ""
    property bool compact:false
    property string suffix:""
    property string editContext:""
    property string lowLabel:""
    property string highLabel:""
    property bool hue:false
    readonly property var liveValue:kind==="range"?range.displayedValue:value
    signal edited(var newValue)
    signal triggered()
    spacing: 4
    Layout.minimumHeight:kind==="text" || kind==="paragraph"?24:40
    Label { textFormat:Text.PlainText; visible:field.kind==="paragraph";text:String(field.value || "");color:theme.muted;font.pixelSize:13;wrapMode:Text.WordWrap;Layout.fillWidth:true }
    RowLayout {
    visible:field.kind!=="paragraph"
    Layout.fillWidth:true;spacing:16
    ColumnLayout {
        Layout.fillWidth: true
        Layout.minimumWidth:0
        Label { textFormat:Text.PlainText; text:field.label; color:field.kind==="text"?theme.muted:theme.text; font.pixelSize:field.compact?13:14; wrapMode:Text.WordWrap; Layout.fillWidth:true }
        Label { textFormat:Text.PlainText; text:field.note; visible:text.length>0; color:theme.muted; font.pixelSize:12; wrapMode:Text.WordWrap; Layout.fillWidth:true }
    }
    Loader {
        id: control
        visible:field.kind!=="options"
        Layout.preferredWidth: field.kind === "toggle" ? 46 : field.compact?180:200
        Layout.minimumWidth:0
        Layout.maximumWidth:field.kind==="toggle"?46:field.compact?180:300
        sourceComponent: field.kind === "toggle" ? toggle : field.kind === "choice" ? choice : field.kind === "number" ? number : field.kind === "button" ? button : field.kind==="range"?rangeValue:info
    }
    }
    UiSlider {
        id:range;visible:field.kind==="range";Layout.fillWidth:true
        from:field.minimum;to:field.maximum;stepSize:field.step;value:field.kind==="range"?Number(field.value || 0):0
        contextKey:field.editContext;accessibleName:field.label;hue:field.hue
        onCommitted:function(value){field.edited(value);}
    }
    RowLayout {
        visible:field.kind==="range" && (field.lowLabel.length>0 || field.highLabel.length>0);Layout.fillWidth:true
        Label { textFormat:Text.PlainText; text:field.lowLabel;color:theme.muted;font.pixelSize:11;Layout.fillWidth:true }
        Label { textFormat:Text.PlainText; text:field.highLabel;color:theme.muted;font.pixelSize:11 }
    }
    UiOptions {
        visible:field.kind==="options";Layout.fillWidth:true
        choices:field.kind==="options"?field.choices:[];value:field.value
        onSelected:function(value){field.edited(value);}
    }
    Component { id: toggle; UiSwitch { checked:!!field.value; onClicked:field.edited(checked); Accessible.name:field.label } }
    Component { id: choice; UiComboBox {
        model:field.choices
        textRole:"label"; valueRole:"id"
        currentIndex: { for(let i=0;i<field.choices.length;i++) if(field.choices[i].id===field.value) return i; return -1; }
        onActivated:field.edited(currentValue)
        Accessible.name:field.label
    } }
    Component { id: number; UiSpinBox {
        objectName:"nativeNumber"
        from:field.minimum; to:field.maximum; stepSize:field.step
        value: Math.max(field.minimum,Math.min(field.maximum,Number(field.value || 0))); editable:true
        onValueModified:field.edited(value)
        Accessible.name:field.label
    } }
    Component { id: button; UiButton { text:String(field.value || field.label); onClicked:field.triggered(); Accessible.name:field.label } }
    Component { id: rangeValue; Label { textFormat:Text.PlainText; text:Math.round(range.displayedValue)+field.suffix; horizontalAlignment:Text.AlignRight;color:theme.text;font.pixelSize:14;font.weight:Font.DemiBold } }
    Component { id: info; Label { textFormat:Text.PlainText; text:field.value === undefined || field.value === null ? "—" : String(field.value); horizontalAlignment:Text.AlignRight; wrapMode:Text.Wrap; color:theme.text; font.pixelSize:field.compact?12:13 } }
}
