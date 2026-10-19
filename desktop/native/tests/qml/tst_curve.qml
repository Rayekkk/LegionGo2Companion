// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtTest
import "../.."
TestCase {
    name:"NativeCurve"; when:windowShown; width:800; height:400
    visible:running; z:1000
    CurveGraph { id:graph; width:800; height:340; curve:[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]]
        onEdited:function(points) { curve=points; } }
    function init() { graph.curve=[[0,0],[45,1500],[55,2000],[65,2600],[75,3200],[85,3900],[95,4500],[100,4800]]; graph.selected=0;graph.editing=false;graph.writable=true;graph.forceActiveFocus(); }
    function test_focusDoesNotEdit() { verify(graph.activeFocus); keyClick(Qt.Key_Right); compare(graph.curve[0][0],0); verify(!graph.editing); }
    function test_activationAndSteps() {
        keyClick(Qt.Key_Return); verify(graph.editing); keyClick(Qt.Key_Right); compare(graph.curve[0][0],1);
        keyClick(Qt.Key_E);compare(graph.selected,1);keyClick(Qt.Key_Up);compare(graph.curve[1][1],1600);
        keyClick(Qt.Key_Escape);verify(!graph.editing);
    }
    function test_orderAndLastPointSpeedOnly() {
        graph.editPoint(1,99,4500);compare(graph.curve[1][0],54);compare(graph.curve[1][1],2000);
        graph.editPoint(7,50,0);compare(graph.curve[7][0],100);compare(graph.curve[7][1],4500,"Point 8 cannot drop below point 7");
        graph.editPoint(7,50,9999);compare(graph.curve[7][0],100);compare(graph.curve[7][1],4800);
        graph.curve=[[0,0],[10,0],[20,0],[30,0],[40,0],[50,0],[95,0],[100,4800]];
        graph.editPoint(7,100,2000);compare(graph.curve[7][1],2000,"Point 8 moves down to its own floor");
        for(const v of [0,600,1500,1999]) { graph.editPoint(7,100,v);compare(graph.curve[7][1],2000,"Point 8 never runs below 2000 RPM"); }
        graph.editPoint(6,95,4000);compare(graph.curve[6][1],2000,"Point 7 cannot rise above point 8");
        graph.curve=[[0,0],[10,0],[20,0],[30,0],[40,0],[50,0],[95,0],[100,2050]];
        graph.selected=7;graph.editing=true;keyClick(Qt.Key_Down);compare(graph.curve[7][1],2000);
        keyClick(Qt.Key_Down);compare(graph.curve[7][1],2000);keyClick(Qt.Key_Left);compare(graph.curve[7][0],100);
    }
    function test_lastPointCanBeDraggedVertically() {
        mousePress(graph,graph.pointX(100),graph.pointY(4800));verify(graph.editing);
        mouseMove(graph,graph.pointX(90),graph.pointY(4600));
        mouseRelease(graph,graph.pointX(90),graph.pointY(4600));
        compare(graph.selected,7);compare(graph.curve[7][0],100);
        const rpmPerPixel=graph.maximum/(graph.pointY(0)-graph.pointY(graph.maximum));
        verify(Math.abs(graph.curve[7][1]-4600)<=rpmPerPixel);
    }
    function test_stopAndDeadband() {
        graph.editPoint(1,45,600);compare(graph.curve[1][1],0);
        graph.editPoint(1,45,1400);compare(graph.curve[1][1],1500);
    }
    function test_pointerDragEditsTheChosenPointAndReleaseEndsTheGesture() {
        mousePress(graph,graph.pointX(45),graph.pointY(1500));
        mouseMove(graph,graph.pointX(50),graph.pointY(1800));
        mouseRelease(graph,graph.pointX(50),graph.pointY(1800));
        compare(graph.selected,1);compare(graph.curve[1][0],50);
        const rpmPerPixel=graph.maximum/(graph.pointY(0)-graph.pointY(graph.maximum));
        verify(Math.abs(graph.curve[1][1]-1800)<=rpmPerPixel,"Pointer speed is accurate within native pixel rounding");
        const saved=JSON.stringify(graph.curve);
        mouseMove(graph,graph.pointX(52),graph.pointY(1900));
        compare(JSON.stringify(graph.curve),saved,"Hover after release cannot keep editing");
    }
    function test_oneDragFollowsEveryMoveUntilRelease() {
        mousePress(graph,graph.pointX(45),graph.pointY(1500));
        for(const t of [48,50,52,54]) { mouseMove(graph,graph.pointX(t),graph.pointY(1700));compare(graph.curve[1][0],t,"Move to "+t+" C"); }
        mouseRelease(graph,graph.pointX(54),graph.pointY(1700));
    }
    function test_readOnlyGraphRejectsPointerAndKeyboardEdits() {
        graph.writable=false;
        const saved=JSON.stringify(graph.curve);
        keyClick(Qt.Key_Return);keyClick(Qt.Key_Right);verify(!graph.editing);
        mousePress(graph,graph.pointX(45),graph.pointY(1500));
        mouseMove(graph,graph.pointX(50),graph.pointY(1800));
        mouseRelease(graph,graph.pointX(50),graph.pointY(1800));
        compare(JSON.stringify(graph.curve),saved);
    }
    function test_everyCurvePointCanBeHitWithAFinger() {
        const hits=[];
        (function collect(item) { if(item.objectName==="curvePointHit")hits.push(item); for(const child of item.children || [])collect(child); })(graph);
        compare(hits.length,graph.curve.length);
        for(const hit of hits) { verify(hit.width>=48,"hit width "+hit.width); verify(hit.height>=48,"hit height "+hit.height); }
        // 20 px beside the drawn dot still grabs the point.
        mousePress(graph,graph.pointX(45)+20,graph.pointY(1500)-20);compare(graph.selected,1);verify(graph.editing);
        mouseRelease(graph,graph.pointX(45)+20,graph.pointY(1500)-20);
    }
    function test_percentFormatting() { graph.unit="percent";compare(graph.displaySpeed(2400),"50.0%");graph.unit="rpm";compare(graph.displaySpeed(2400),"2400 RPM"); }
}
