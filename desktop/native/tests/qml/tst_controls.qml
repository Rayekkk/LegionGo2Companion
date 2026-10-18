// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtTest
import "../.."
TestCase {
    name:"StyledNativeControls"; when:windowShown; width:700; height:400
    // Other cases have persistent curve items at the same origin. Keep this
    // case's pointer surface on top while it runs, then remove it from picking.
    visible:running; z:1000
    UiSwitch { id:toggle; x:20; y:20; checked:false }
    UiComboBox { id:choice; x:20; y:160; textRole:"label"; valueRole:"id"; model:[{id:1,label:"One"},{id:2,label:"Two"}]; currentIndex:0 }
    UiButton { id:button; x:20; y:230; text:"Save"; tone:"primary"; property int calls:0; onClicked:calls++ }
    UiSlider { id:range;x:300;y:40;width:300;from:0;to:100;value:20;property var commits:[];onCommitted:function(value){commits=commits.concat(value);} }
    UiOptions { id:segments;x:300;y:110;width:300;choices:[{id:"a",label:"First"},{id:"b",label:"Second"}];value:"a";property var commits:[];onSelected:function(value){commits=commits.concat(value);} }
    UiField { id:readout;x:300;y:200;width:300;value:"<b>Device supplied text</b>" }
    function init() { toggle.checked=false;choice.currentIndex=0;choice.popup.close();button.calls=0; }
    function test_switchKeepsNativeKeyboardAndPointerActivation() {
        mouseClick(toggle,toggle.width/2,toggle.height/2);compare(toggle.checked,true);
        toggle.forceActiveFocus();keyClick(Qt.Key_Space);compare(toggle.checked,false);
    }
    function test_dynamicReadoutsNeverInterpretMarkup() {
        function matching(item) {
            if(item.text===readout.value && item.textFormat!==undefined)return item;
            for(let i=0;i<(item.children || []).length;i++) {
                const child=matching(item.children[i]);if(child)return child;
            }
            return null;
        }
        const label=matching(readout);verify(label!==null);
        compare(label.textFormat,Text.PlainText);
    }
    function test_dropdownPreservesIdsAndKeyboardSelection() {
        choice.forceActiveFocus();keyClick(Qt.Key_Space);tryCompare(choice.popup,"visible",true);
        keyClick(Qt.Key_Down);keyClick(Qt.Key_Return);compare(choice.currentValue,2);
        compare(choice.displayText,"Two");
    }
    function test_buttonDoesNotActivateWhenDisabled() {
        mouseClick(button,button.width/2,button.height/2);compare(button.calls,1);
        button.enabled=false;mouseClick(button,button.width/2,button.height/2);compare(button.calls,1);button.enabled=true;
    }
    function test_sliderCommitsOnceAfterDragAndDebouncesKeyboard() {
        range.from=0;range.to=100;range.value=20;range.enabled=true;range.commits=[];range.contextKey="global";
        const slider=findChild(range,"nativeSlider");verify(slider!==null);
        mousePress(slider,65,15);mouseMove(slider,180,15);mouseMove(slider,230,15);
        compare(range.commits.length,0);verify(range.displayedValue>60);
        mouseRelease(slider,230,15);compare(range.commits.length,1);verify(range.commits[0]>60);
        range.value=range.commits[0];range.commits=[];slider.forceActiveFocus();
        keyClick(Qt.Key_Right);keyClick(Qt.Key_Right);compare(range.commits.length,0);
        tryVerify(function(){return range.commits.length===1;},1500);
        compare(range.commits[0],range.value+2);
    }
    function test_sliderCancelsWhenProfileChangesOrExternalStateArrives() {
        range.from=0;range.to=100;range.enabled=true;range.value=20;range.commits=[];range.contextKey="old";
        const slider=findChild(range,"nativeSlider");
        mousePress(slider,65,15);mouseMove(slider,220,15);range.contextKey="new";
        mouseRelease(slider,220,15);compare(range.commits.length,0);
        mousePress(slider,65,15);mouseMove(slider,220,15);range.value=40;
        mouseRelease(slider,220,15);compare(range.commits.length,0);compare(range.displayedValue,40);
        slider.forceActiveFocus();keyClick(Qt.Key_Right);range.enabled=false;
        wait(1500);compare(range.commits.length,0); // Covers the same allowed debounce window as the positive case.
        range.enabled=true;
    }
    function test_sliderRebindsWhenCapabilitiesArrive() {
        range.value=15;range.from=5;range.to=5;
        const slider=findChild(range,"nativeSlider");compare(slider.value,5);
        range.to=30;tryCompare(slider,"value",15);
    }
    function test_visibleOptionsPreserveIdsAndRespectDisabledState() {
        segments.commits=[];segments.enabled=true;
        const second=findChild(segments,"option_b");verify(second!==null);
        mouseClick(second,second.width/2,second.height/2);compare(segments.commits[0],"b");
        segments.enabled=false;mouseClick(second,second.width/2,second.height/2);compare(segments.commits.length,1);segments.enabled=true;
    }
}
