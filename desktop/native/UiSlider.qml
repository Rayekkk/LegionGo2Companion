// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
Item {
    id:control
    UiTheme { id:theme }
    property real from:0
    property real to:100
    property real stepSize:1
    property real value:0
    property real draft:value
    property string contextKey:""
    property string accessibleName:""
    property bool hue:false
    property bool pending:false
    property bool invalidated:false
    readonly property real displayedValue:pending?draft:value
    signal committed(real value)
    implicitHeight:theme.target; implicitWidth:240
    opacity:enabled?1:0.42
    function cancel() { settle.stop();pending=false;invalidated=true;draft=value; }
    function flush() {
        settle.stop();
        if(!pending || invalidated || !enabled || !visible)return;
        const next=draft;pending=false;
        if(next!==value)committed(next);
        // The next backend snapshot remains authoritative after an edit.
        draft=value;
    }
    onValueChanged: { if(pending)cancel();else draft=value; }
    onContextKeyChanged:cancel()
    onEnabledChanged:if(!enabled)cancel()
    onVisibleChanged:if(!visible)cancel()
    Timer { id:settle;interval:180;onTriggered:control.flush() }
    Slider {
        id:slider;objectName:"nativeSlider"
        anchors.fill:parent;from:control.from;to:control.to;stepSize:control.stepSize
        value:Math.max(control.from,Math.min(control.to,control.draft));live:true;snapMode:Slider.SnapAlways
        leftPadding:9;rightPadding:9;hoverEnabled:true
        Accessible.name:control.accessibleName
        Keys.onPressed:function(event) {
            let next=control.pending?control.draft:control.value;
            if(event.key===Qt.Key_Right || event.key===Qt.Key_Up)next+=stepSize;
            else if(event.key===Qt.Key_Left || event.key===Qt.Key_Down)next-=stepSize;
            else if(event.key===Qt.Key_Home)next=from;
            else if(event.key===Qt.Key_End)next=to;
            else return;
            control.invalidated=false;control.draft=Math.max(from,Math.min(to,next));
            control.pending=true;settle.restart();event.accepted=true;
        }
        onPressedChanged: {
            if(pressed){settle.stop();control.invalidated=false;}
            else control.flush();
        }
        onMoved: {
            if(!pressed)control.invalidated=false;
            if(control.invalidated)return;
            control.draft=value;control.pending=true;
            if(!pressed)settle.restart();
        }
        background:Rectangle {
            x:slider.leftPadding;y:(slider.height-height)/2;width:slider.availableWidth;height:6;radius:3;color:theme.border
            Canvas {
                anchors.fill:parent;visible:control.hue
                onPaint: { const c=getContext("2d"),g=c.createLinearGradient(0,0,width,0);for(let i=0;i<=6;i++)g.addColorStop(i/6,Qt.hsva(i/6,1,1,1));c.fillStyle=g;c.fillRect(0,0,width,height); }
                onWidthChanged:requestPaint()
            }
            Rectangle { visible:!control.hue;width:slider.visualPosition*parent.width;height:parent.height;radius:3;color:theme.accentText }
        }
        handle:Rectangle {
            x:slider.leftPadding+slider.visualPosition*(slider.availableWidth-width);y:(slider.height-height)/2
            width:28;height:28;radius:14;color:control.hue?Qt.hsva(slider.value/359,1,1,1):theme.accentText
            border.width:slider.activeFocus?3:2;border.color:slider.activeFocus?theme.text:theme.card
        }
    }
}
