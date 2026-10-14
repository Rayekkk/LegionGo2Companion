// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Shapes
FocusScope {
    id:graph
    UiTheme { id:theme }
    objectName:"curveGraph"
    property var curve:[]
    property int maximum:4800
    property int minimumRunning:1500
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
    Accessible.name:"Fan curve. Activate to edit. Left and right change temperature. Up and down change speed. Q and E select points. Escape ends editing."
    function displaySpeed(rpm) { return unit==="percent"?Math.round(100*rpm/maximum)+"%":Math.round(rpm)+" RPM"; }
    function pointX(t) { return plot.x+plot.width*Math.max(0,Math.min(100,t))/100; }
    function pointY(r) { return plot.y+plot.height*(1-Math.max(0,Math.min(maximum,r))/maximum); }
    function editPoint(index, temp, speed) {
        if (!writable || index<0 || index>=curve.length-1) return;
        const points=curve.map(p=>[p[0],p[1]]);
        const loT=index?points[index-1][0]+1:0, hiT=points[index+1][0]-1;
        const loR=index?points[index-1][1]:0, hiR=points[index+1][1];
        temp=Math.max(loT,Math.min(hiT,Math.round(temp)));
        speed=Math.max(loR,Math.min(hiR,Math.round(speed)));
        if (speed>0 && speed<minimumRunning) speed=speed>=minimumRunning/2?minimumRunning:0;
        speed=Math.max(loR,Math.min(hiR,speed));
        points[index]=[temp,speed]; edited(points);
    }
    function nudge(dx,dy) {
        if (!curve[selected]) return;
        let speed=curve[selected][1]+dy*speedStep;
        if (dy>0 && curve[selected][1]===0) speed=minimumRunning;
        if (dy<0 && speed<minimumRunning) speed=0;
        editPoint(selected,curve[selected][0]+dx,speed);
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
            Label { textFormat:Text.PlainText; x:-66; y:-8; width:56; horizontalAlignment:Text.AlignRight; color:theme.muted; font.pixelSize:12; text:graph.unit==="percent"?(100-index*20)+"%":Math.round(graph.maximum*(1-index/5)) }
        }
    }
    Repeater {
        model:11
        delegate:Item {
            required property int index
            x:plot.x+plot.width*index/10; y:plot.y; height:plot.height
            Rectangle { height:parent.height; width:1; color:theme.input }
            Label { textFormat:Text.PlainText; x:-18; y:plot.height+12; width:36; horizontalAlignment:Text.AlignHCenter; color:theme.muted; font.pixelSize:12; text:(index*10)+"°" }
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
        model:graph.curve
        delegate:Rectangle {
            required property int index
            required property var modelData
            width:index===graph.selected?16:12; height:width; radius:width/2
            x:graph.pointX(modelData[0])-width/2; y:graph.pointY(modelData[1])-height/2
            color:index===graph.selected?theme.text:theme.accentText; border.width:2; border.color:theme.sidebar
            MouseArea {
                anchors { fill:parent; margins:-8 } cursorShape:index===graph.curve.length-1?Qt.ArrowCursor:Qt.SizeAllCursor
                onPressed: { graph.selected=index; graph.forceActiveFocus(); graph.editing=graph.writable && index<graph.curve.length-1; }
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
        horizontalAlignment:Text.AlignHCenter; color:graph.editing?theme.text:theme.muted; font.pixelSize:12
        text:graph.curve.length ? "Point " +(graph.selected+1)+": "+graph.curve[graph.selected][0]+"°C · "+graph.displaySpeed(graph.curve[graph.selected][1])+
             (graph.selected===graph.curve.length-1?" · fixed maximum":graph.editing?" · ← → temperature   ↑ ↓ speed   Q / E point   Esc done":" · Enter to edit, or drag a point") : "Waiting for fan settings"
        wrapMode:Text.WordWrap
    }
}
