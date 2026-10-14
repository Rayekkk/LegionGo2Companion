// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
ComboBox {
    id:control
    UiTheme { id:theme }
    implicitHeight:40; implicitWidth:210
    leftPadding:13; rightPadding:32; hoverEnabled:true
    opacity:enabled?1:0.42
    font.pixelSize:13
    contentItem:Text { textFormat:Text.PlainText; text:control.displayText; font:control.font; color:theme.text; verticalAlignment:Text.AlignVCenter; elide:Text.ElideRight }
    indicator:Text { textFormat:Text.PlainText; x:control.width-width-13; y:(control.height-height)/2; text:"⌄"; color:theme.muted; font.pixelSize:18 }
    background:Rectangle { radius:8; color:control.hovered?theme.inputHover:theme.input; border.color:control.activeFocus?theme.accentText:theme.border; border.width:control.activeFocus?2:1 }
    delegate:ItemDelegate {
        id:option
        required property int index
        width:control.width-12; height:38
        highlighted:control.highlightedIndex===index
        text:control.textAt(index)
        contentItem:Text { textFormat:Text.PlainText; text:option.text; color:option.highlighted?"#ffffff":theme.text; font.pixelSize:13; verticalAlignment:Text.AlignVCenter; elide:Text.ElideRight }
        background:Rectangle { radius:7; color:option.highlighted?theme.accent:option.hovered?theme.input:"transparent" }
    }
    popup:Popup {
        y:control.height+6; width:control.width; padding:6
        implicitHeight:Math.min(contentItem.implicitHeight+12,300)
        contentItem:ListView { clip:true; implicitHeight:contentHeight; model:control.popup.visible?control.delegateModel:null; currentIndex:control.highlightedIndex; ScrollIndicator.vertical:ScrollIndicator {} }
        background:Rectangle { radius:12; color:theme.card; border.color:theme.border }
    }
}
