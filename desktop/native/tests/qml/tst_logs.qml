// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Window
import QtTest
import "../.."
TestCase {
    name:"LogBrowser";when:windowShown;width:1100;height:1000
    visible:running;z:1000
    QtObject {
        id:fake
        property var records:[]
        property bool loading:false
        property bool stale:false
        property bool exporting:false
        property string error:""
        property string status:""
        property string exportedPath:""
        property var warnings:[]
        property string search:""
        property string level:"all"
        property string source:"all"
        property string component:"all"
        property int ageMinutes:0
        property var sourceOptions:["backend","desktop"]
        property var componentOptions:["fan","runtime"]
        property int filteredCount:records.length
        property int totalCount:records.length
        property int refreshes:0
        property int exports:0
        signal filtersChanged()
        function refresh(){refreshes++;}
        function exportFiltered(){exports++;exportedPath="/home/deck/Desktop/LegionGoCompanion-logs.jsonl";}
    }
    Component { id:factory;LogsPage { width:1050;viewer:fake } }
    property var view
    function init(){
        fake.records=[];fake.refreshes=0;fake.exports=0;fake.loading=false;fake.error="";fake.status="";fake.warnings=[];fake.exportedPath="";
        fake.search="";fake.level="all";fake.source="all";fake.component="all";fake.ageMinutes=0;
        view=createTemporaryObject(factory,this);verify(view!==null);
        view.Window.window.width=1100;view.Window.window.height=1000;
        waitForRendering(view);
        tryCompare(fake,"refreshes",1);
    }
    function test_empty_export_is_disabled_and_refresh_does_not_arm_controls(){
        verify(!findChild(view,"logsExport").enabled);
        mouseClick(findChild(view,"logsRefresh"));compare(fake.refreshes,2);
        fake.loading=true;verify(!findChild(view,"logsRefresh").enabled);
    }
    function test_filters_search_and_visible_export(){
        fake.records=[{time:"2026-10-14T12:00:00Z",event:"transport_failed",component:"fan",source:"backend",level:"error",error:{errno:104}}];
        verify(findChild(view,"logsExport").enabled);
        const search=findChild(view,"logsSearch");search.forceActiveFocus();keyClick(Qt.Key_1);keyClick(Qt.Key_0);keyClick(Qt.Key_4);
        tryCompare(fake,"search","104");
        mouseClick(findChild(view,"logsExport"));compare(fake.exports,1);
        verify(findChild(view,"logsExportedPath").text.includes("Desktop"));
    }
    function test_stale_and_partial_results_remain_visible(){
        fake.stale=true;fake.error="Backend logs could not be refreshed.";fake.status="Showing the previous snapshot.";fake.warnings=["journal_unavailable"];
        verify(findChild(view,"logsStatus").text.includes("previous snapshot"));
        const list=findChild(view,"logsList");fake.records=[{time:"2026-10-14T12:00:00Z",event:"test",component:"runtime",source:"desktop",level:"warning"}];
        tryCompare(list,"count",1);
    }
    function test_reopen_keeps_search_visible(){
        fake.search="104";
        compare(findChild(view,"logsSearch").text,"104");
        view.destroy();view=createTemporaryObject(factory,this);verify(view!==null);
        compare(findChild(view,"logsSearch").text,"104");
    }
    function test_selected_event_survives_focus_and_clears_with_new_results(){
        const list=findChild(view,"logsList");
        fake.records=[{time:"2026-10-14T12:00:00Z",event:"transport_failed",component:"fan",source:"backend",level:"error"}];
        tryCompare(list,"count",1);
        mouseClick(list,250,30);
        compare(view.selectedRecord.event,"transport_failed");compare(view.selectedIndex,0);
        findChild(view,"logsSearch").forceActiveFocus();
        compare(view.selectedRecord.event,"transport_failed");compare(view.selectedIndex,0);
        fake.filtersChanged();compare(view.selectedRecord,null);compare(view.selectedIndex,-1);
        mouseClick(list,250,30);compare(view.selectedIndex,0);
        fake.records=[{time:"2026-10-14T12:01:00Z",event:"reconnected",component:"fan",source:"backend",level:"info"}];
        tryCompare(view,"selectedRecord",null);compare(view.selectedIndex,-1);
    }
}
