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
    property string prefix:""
    property string suffix:""
    property string editContext:""
    property string lowLabel:""
    property string highLabel:""
    property bool hue:false
    property string badge:""  // A paragraph led by a badge (the Fan page's Beta row).
    readonly property var liveValue:kind==="range"?range.displayedValue:value
    // A button that already says what it does leads its row; its note follows it.
    readonly property bool bare:kind==="button" && (label.length===0 || String(value)===label)
    signal edited(var newValue)
    signal triggered()
    spacing: 4
    Layout.minimumHeight:kind==="text" || kind==="paragraph"?24:theme.target
    Label { textFormat:Text.PlainText; visible:field.kind==="paragraph" && !field.badge;text:String(field.value || "");color:theme.muted;font.pixelSize:theme.fontSmall;wrapMode:Text.WordWrap;Layout.fillWidth:true }
    RowLayout { visible:field.kind==="paragraph" && !!field.badge; Layout.fillWidth:true; spacing:12
        UiBadge { text:field.badge; tone:"amber"; Layout.alignment:Qt.AlignTop }
        Label { textFormat:Text.PlainText; text:String(field.value || ""); color:theme.muted; wrapMode:Text.WordWrap; Layout.fillWidth:true }
    }
    RowLayout {
    visible:field.kind!=="paragraph" && field.kind!=="list" && (field.kind!=="buttons" || field.label.length>0)
    Layout.fillWidth:true;spacing:16
    layoutDirection:field.bare?Qt.RightToLeft:Qt.LeftToRight
    ColumnLayout {
        Layout.fillWidth: true
        Layout.minimumWidth:0
        Label { textFormat:Text.PlainText; text:field.label; visible:!field.bare; color:field.kind==="text"?theme.muted:theme.text; font.pixelSize:theme.font; wrapMode:Text.WordWrap; Layout.fillWidth:true }
        Label { textFormat:Text.PlainText; text:field.note; visible:text.length>0; color:theme.muted; font.pixelSize:theme.fontSmall; wrapMode:Text.WordWrap; Layout.fillWidth:true }
    }
    Loader {
        id: control
        visible:field.kind!=="options" && field.kind!=="buttons"
        Layout.preferredWidth: field.kind === "toggle" ? 60 : field.kind==="range" ? 80 : field.kind==="button" ? Math.max(140,implicitWidth) : 220
        Layout.minimumWidth:0
        Layout.maximumWidth:field.kind==="toggle"?60:320
        sourceComponent: field.kind === "toggle" ? toggle : field.kind === "choice" ? choice : field.kind === "button" ? button : field.kind==="range"?rangeValue:info
    }
    }
    // Rows of a title and a muted detail line, e.g. the saved game profiles.
    Label { textFormat:Text.PlainText; visible:field.kind==="list" && !(field.value || []).length;text:"No game has its own settings yet.";color:theme.muted;font.pixelSize:theme.fontSmall }
    readonly property int listCount:rows.count
    Repeater {
        id:rows
        model:field.kind==="list" && Array.isArray(field.value)?field.value:[]
        delegate:RowLayout {
            id:row
            required property var modelData
            Layout.fillWidth:true;spacing:10
            ColumnLayout {
                Layout.fillWidth:true;Layout.minimumWidth:0;spacing:2
                Label { textFormat:Text.PlainText;text:row.modelData.title;color:theme.text;font.pixelSize:theme.font;elide:Text.ElideRight;Layout.fillWidth:true }
                Label { textFormat:Text.PlainText;text:row.modelData.detail;color:theme.muted;font.pixelSize:theme.fontSmall;wrapMode:Text.WordWrap;Layout.fillWidth:true }
            }
            // Row actions report {id, action} through edited().
            Repeater {
                model:row.modelData.actions || []
                delegate:UiButton {
                    required property var modelData
                    objectName:"row_"+row.modelData.id+"_"+modelData.id
                    text:modelData.label;tone:modelData.tone || "secondary";enabled:modelData.enabled!==false
                    onClicked:field.edited({id:row.modelData.id,action:modelData.id})
                }
            }
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
        Label { textFormat:Text.PlainText; text:field.lowLabel;color:theme.muted;font.pixelSize:theme.fontTiny;Layout.fillWidth:true }
        Label { textFormat:Text.PlainText; text:field.highLabel;color:theme.muted;font.pixelSize:theme.fontTiny }
    }
    UiOptions {
        visible:field.kind==="options";Layout.fillWidth:true
        choices:field.kind==="options"?field.choices:[];value:field.value
        onSelected:function(value){field.edited(value);}
    }
    // A row of actions with an optional status (e.g. "Unsaved changes") after them.
    Flow {
        visible:field.kind==="buttons";Layout.fillWidth:true;spacing:10
        Repeater {
            model:field.kind==="buttons"?field.choices:[]
            delegate:UiButton {
                required property var modelData
                objectName:"button_"+modelData.id
                text:modelData.label;tone:modelData.tone || "secondary";enabled:modelData.enabled!==false
                onClicked:field.edited(modelData.id)
            }
        }
        Label { textFormat:Text.PlainText;text:field.kind==="buttons"?String(field.value || ""):"";visible:text.length>0;color:theme.warning;height:theme.target;verticalAlignment:Text.AlignVCenter;font.pixelSize:theme.fontSmall }
    }
    // The switch shows the confirmed value: a cancelled confirmation or a failed write flips it back.
    Component { id: toggle; UiSwitch { checked:!!field.value; onClicked: { const next=checked; checked=Qt.binding(function(){return !!field.value;}); field.edited(next); } Accessible.name:field.label } }
    Component { id: choice; UiComboBox {
        model:field.choices
        textRole:"label"; valueRole:"id"
        currentIndex: { for(let i=0;i<field.choices.length;i++) if(field.choices[i].id===field.value) return i; return -1; }
        onActivated:field.edited(currentValue)
        Accessible.name:field.label
    } }
    Component { id: button; UiButton { text:String(field.value || field.label); onClicked:field.triggered(); Accessible.name:field.label } }
    Component { id: rangeValue; Label { textFormat:Text.PlainText; text:field.prefix+Math.round(range.displayedValue)+field.suffix; horizontalAlignment:Text.AlignRight;color:theme.text;font.pixelSize:theme.font;font.weight:Font.DemiBold } }
    Component { id: info; Label { textFormat:Text.PlainText; text:field.value === undefined || field.value === null ? "—" : String(field.value); horizontalAlignment:Text.AlignRight; wrapMode:Text.Wrap; color:theme.text; font.pixelSize:theme.font } }
}
