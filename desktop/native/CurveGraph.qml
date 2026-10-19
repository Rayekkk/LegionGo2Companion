// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Shapes
import "fan.mjs" as Fan
FocusScope {
    id:graph
    UiTheme { id:theme }
    objectName:"curveGraph"
    property var curve:[]
    property int maximum:4800
    property int minimumRunning:1500
    property int lastMinimum:2000
    property int speedStep:100
    property string unit:"rpm"
    property int selected:0
    property bool editing:false
    property bool writable:true
    property bool liveValid:false
    property real liveTemperature:0
    property real liveRpm:0
    signal edited(var points)
    implicitHeight:310
    activeFocusOnTab:true
    Accessible.role:Accessible.Chart
    Accessible.name:"Fan curve. Activate to edit. Left and right change temperature. Up and down change speed. Q and E select points. The 100 °C point changes speed only. Escape ends editing."
    function displaySpeed(rpm) { return Fan.speedText(rpm,unit,maximum); }
    readonly property var limits:({min_rpm:minimumRunning,last_min_rpm:lastMinimum,max_rpm:maximum,step:1})
    function pointX(t) { return plot.x+plot.width*Math.max(0,Math.min(100,t))/100; }
    function pointY(r) { return plot.y+plot.height*(1-Math.max(0,Math.min(maximum,r))/maximum); }
    function editPoint(index, temp, speed) {
        if (!writable || index<0 || index>=curve.length) return;
        const points=Fan.editPoint(Fan.editPoint(curve,index,0,temp,limits),index,1,speed,limits);
        if (points[index][0]!==curve[index][0] || points[index][1]!==curve[index][1]) edited(points);
    }
    function nudge(dx,dy) {
        if (!curve[selected]) return;
        editPoint(selected,curve[selected][0]+dx,Fan.nudgeSpeed(curve[selected][1],dy*speedStep,minimumRunning));
    }
    Keys.onPressed: function(event) {
        if (!editing && (event.key===Qt.Key_Return || event.key===Qt.Key_Enter || event.key===Qt.Key_Space)) {
            editing=writable; event.accepted=true; return;
        }
        if (!editing) return;
        if (event.key===Qt.Key_Escape) { editing=false; event.accepted=true; }
        else if (event.key===Qt.Key_Q || event.key===Qt.Key_PageUp) { selected=Math.max(0,selected-1); event.accepted=true; }
        else if (event.key===Qt.Key_E || event.key===Qt.Key_PageDown) { selected=Math.min(curve.length-1,selected+1); event.accepted=true; }
        else if (event.key===Qt.Key_Left) { nudge(-1,0); event.accepted=true; }
        else if (event.key===Qt.Key_Right) { nudge(1,0); event.accepted=true; }
        else if (event.key===Qt.Key_Up) { nudge(0,1); event.accepted=true; }
        else if (event.key===Qt.Key_Down) { nudge(0,-1); event.accepted=true; }
    }
    Rectangle { anchors.fill:parent; radius:14; color:theme.sidebar; border.color:graph.activeFocus?theme.accentText:theme.border; border.width:graph.editing?2:1 }
    Item { id:plot; x:70; y:26; width:parent.width-96; height:parent.height-98 }
    Rectangle { x:plot.x; y:graph.pointY(graph.minimumRunning); width:plot.width; height:plot.y+plot.height-y; color:theme.warning; opacity:0.035 }
    Repeater {
        model:6
        delegate:Item {
            required property int index
            x:plot.x; y:plot.y+plot.height*index/5; width:plot.width
            Rectangle { width:parent.width; height:1; color:theme.divider }
            Label { textFormat:Text.PlainText; x:-66; y:-8; width:56; horizontalAlignment:Text.AlignRight; color:theme.muted; font.pixelSize:13; text:graph.unit==="percent"?(100-index*20)+"%":Math.round(graph.maximum*(1-index/5)) }
        }
    }
    Repeater {
        model:11
        delegate:Item {
            required property int index
            x:plot.x+plot.width*index/10; y:plot.y; height:plot.height
            Rectangle { height:parent.height; width:1; color:theme.input }
            Label { textFormat:Text.PlainText; x:-18; y:plot.height+12; width:36; horizontalAlignment:Text.AlignHCenter; color:theme.muted; font.pixelSize:13; text:(index*10)+"°" }
        }
    }
    Shape {
        anchors.fill:parent
        preferredRendererType:Shape.CurveRenderer
        ShapePath {
            strokeColor:theme.accentHover; strokeWidth:3; fillColor:"transparent"
            startX:graph.curve.length?graph.pointX(graph.curve[0][0]):plot.x
            startY:graph.curve.length?graph.pointY(graph.curve[0][1]):plot.y+plot.height
            PathPolyline { path:graph.curve.map(p=>Qt.point(graph.pointX(p[0]),graph.pointY(p[1]))) }
        }
    }
    Repeater {
        // A count, not the array: every edit replaces the array, and a new model would
        // destroy the delegate whose MouseArea is still handling the drag.
        model:graph.curve.length
        delegate:Rectangle {
            required property int index
            readonly property var modelData:graph.curve[index]
            width:index===graph.selected?22:16; height:width; radius:width/2
            x:graph.pointX(modelData[0])-width/2; y:graph.pointY(modelData[1])-height/2
            color:index===graph.selected?theme.text:theme.accentText; border.width:2; border.color:theme.sidebar
            MouseArea {
                objectName:"curvePointHit"
                // A finger needs at least 48x48 px whatever the drawn dot size.
                anchors { fill:parent; margins:-(48-parent.width)/2 } cursorShape:index===graph.curve.length-1?Qt.SizeVerCursor:Qt.SizeAllCursor
                onPressed: { graph.selected=index; graph.forceActiveFocus(); graph.editing=graph.writable; }
                onPositionChanged: function(mouse) {
                    if (!pressed || !graph.editing) return;
                    const p=mapToItem(graph,mouse.x,mouse.y);
                    graph.editPoint(index,(p.x-plot.x)*100/plot.width,(1-(p.y-plot.y)/plot.height)*graph.maximum);
                }
            }
        }
    }
    Rectangle {
        visible:graph.liveValid; width:14; height:14; radius:7; color:theme.success; border.color:theme.sidebar; border.width:2
        x:graph.pointX(graph.liveTemperature)-7; y:graph.pointY(graph.liveRpm)-7
    }
    Label { textFormat:Text.PlainText;
        anchors { left:parent.left; right:parent.right; bottom:parent.bottom; margins:12 }
        horizontalAlignment:Text.AlignHCenter; color:graph.editing?theme.text:theme.muted; font.pixelSize:14
        text:graph.curve.length ? Fan.pointText(graph.selected,graph.curve.length,graph.curve[graph.selected][0],graph.displaySpeed(graph.curve[graph.selected][1]))
             : "Waiting for fan settings"
        wrapMode:Text.WordWrap
    }
}
