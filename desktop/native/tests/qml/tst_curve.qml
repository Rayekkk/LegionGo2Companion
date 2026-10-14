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
    function test_orderAndFixedMaximum() {
        graph.editPoint(1,99,4500);compare(graph.curve[1][0],54);compare(graph.curve[1][1],2000);
        graph.editPoint(7,50,0);compare(graph.curve[7][0],100);compare(graph.curve[7][1],4800);
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
    function test_readOnlyGraphRejectsPointerAndKeyboardEdits() {
        graph.writable=false;
        const saved=JSON.stringify(graph.curve);
        keyClick(Qt.Key_Return);keyClick(Qt.Key_Right);verify(!graph.editing);
        mousePress(graph,graph.pointX(45),graph.pointY(1500));
        mouseMove(graph,graph.pointX(50),graph.pointY(1800));
        mouseRelease(graph,graph.pointX(50),graph.pointY(1800));
        compare(JSON.stringify(graph.curve),saved);
    }
    function test_percentFormatting() { graph.unit="percent";compare(graph.displaySpeed(2400),"50%");graph.unit="rpm";compare(graph.displaySpeed(2400),"2400 RPM"); }
}
