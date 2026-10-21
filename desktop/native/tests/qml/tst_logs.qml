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
        property string level:"warning"
        property bool copied:false
        property bool fileSinkUnavailable:false
        readonly property bool exportable:records.length>0
        property int copies:0
        property string source:"all"
        property string component:"all"
        property int ageMinutes:0
        property var sourceOptions:["backend","desktop"]
        property var componentOptions:["fan","runtime"]
        property int filteredCount:records.length
        property int hidden:0
        property int totalCount:records.length+hidden
        property int refreshes:0
        property int exports:0
        signal filtersChanged()
        function refresh(){refreshes++;}
        function copyToClipboard(){copies++;copied=true;}
        function exportFiltered(){exports++;exportedPath="/home/deck/Desktop/LegionGoCompanion-logs.jsonl";}
    }
    QtObject {
        id:bridge
        property bool connected:true
        property bool reportSaving:false
        property string reportText:""
        property var calls:[]
        function saveDeviceReport(){if(reportSaving || !connected)return;reportSaving=true;reportText="";calls=calls.concat([["save_device_report",[]]]);}
        function finish(text){reportSaving=false;reportText=text;}
    }
    Component { id:factory;LogsPage { width:1050;viewer:fake;backend:bridge } }
    property var view
    function init(){
        fake.records=[];fake.refreshes=0;fake.exports=0;fake.loading=false;fake.error="";fake.status="";fake.warnings=[];fake.exportedPath="";
        bridge.connected=true;bridge.reportSaving=false;bridge.reportText="";bridge.calls=[];
        fake.search="";fake.level="warning";fake.copied=false;fake.copies=0;fake.hidden=0;fake.fileSinkUnavailable=false;fake.source="all";fake.component="all";fake.ageMinutes=0;
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
    function row(index){const list=findChild(view,"logsList");tryCompare(list,"count",fake.records.length);return list.itemAtIndex(index);}
    function test_rows_say_what_happened_in_plain_words(){
        fake.records=[
            {time:"2026-10-14T12:00:00Z",event:"startup",component:"plugin",source:"backend",level:"info"},
            {time:"2026-10-14T12:01:00Z",event:"transaction",component:"wifi",source:"wifi",level:"error",context:{operation:"set_band_policy",outcome:"rolled_back",reason:"link_lost"},error:{type:"OSError"}},
            {time:"2026-10-14T12:02:00Z",event:"cpu_power_restore_failed",component:"tdp",source:"backend",level:"warning"}];
        compare(findChild(row(0),"logTitle").text,"Plugin started");
        compare(findChild(row(1),"logTitle").text,"Wi-Fi change");
        compare(findChild(row(1),"logDetail").text,"Wi-Fi · Set band policy · Outcome: rolled back · Reason: link lost · Error: OSError");
        compare(findChild(row(2),"logTitle").text,"Cpu power restore failed");
        compare(findChild(row(2),"logDetail").text,"TDP");
    }
    function test_time_is_shown_in_local_time(){
        fake.records=[{time:"2026-10-14T23:30:00Z",event:"startup",component:"plugin",source:"backend",level:"info"}];
        compare(findChild(row(0),"logTime").text,Qt.formatDateTime(new Date("2026-10-14T23:30:00Z"),"d MMM hh:mm:ss"));
        verify(!findChild(row(0),"logTime").text.includes("T23"));
    }
    function test_problems_are_the_default_and_all_is_one_tap_away(){
        fake.records=[{time:"2026-10-14T12:00:00Z",event:"a",component:"fan",source:"backend",level:"error"}];
        compare(findChild(view,"option_warning").tone,"primary");
        compare(findChild(view,"logsCount").text,"1 problem in 1 collected events");
        mouseClick(findChild(view,"option_all"));compare(fake.level,"all");
        compare(findChild(view,"option_all").tone,"primary");
        compare(findChild(view,"logsCount").text,"1 of 1 collected events");
        mouseClick(findChild(view,"logsReset"));compare(fake.level,"warning");
    }
    function test_empty_problem_list_points_to_all_events(){
        compare(findChild(view,"logsEmpty").text,"No diagnostic events are available.");
        fake.hidden=3;
        compare(findChild(view,"logsEmpty").text,"No warnings or errors. Choose “All events” to see everything.");
        fake.level="all";
        compare(findChild(view,"logsEmpty").text,"No events match these filters.");
    }
    function test_copy_button_copies_and_reports_it(){
        verify(!findChild(view,"logsCopy").enabled);
        fake.records=[{time:"2026-10-14T12:00:00Z",event:"a",component:"fan",source:"backend",level:"error"}];
        verify(findChild(view,"logsCopy").enabled);
        mouseClick(findChild(view,"logsCopy"));compare(fake.copies,1);
        compare(findChild(view,"logsExportedPath").text,"Copied to the clipboard.");
    }
    function test_file_sink_notice_only_when_the_backend_reports_it(){
        verify(!findChild(view,"logsSinkNotice").visible);
        fake.fileSinkUnavailable=true;
        verify(findChild(view,"logsSinkNotice").visible);
        verify(findChild(view,"logsSinkNotice").text.includes("event log file"));
    }
    function test_device_report_saves_through_the_backend_and_shows_where(){
        const button=findChild(view,"logsDeviceReport");
        compare(button.text,"Save device report");
        verify(findChild(view,"logsDeviceReportInfo").text.includes("No serial numbers, network names or user names."));
        verify(button.enabled,"no log records needed");
        mouseClick(button);
        compare(bridge.calls,[["save_device_report",[]]]);
        compare(button.text,"Saving report…");verify(!button.enabled);
        bridge.finish("Saved to /home/deck/Desktop/LegionGoCompanion-device-report-20261021-120000.txt");
        compare(findChild(view,"logsDeviceReportResult").text,"Saved to /home/deck/Desktop/LegionGoCompanion-device-report-20261021-120000.txt");
        compare(button.text,"Save device report");
        mouseClick(button);bridge.finish("The Desktop directory does not belong to the console user.");
        compare(findChild(view,"logsDeviceReportResult").text,"The Desktop directory does not belong to the console user.");
        mouseClick(button);bridge.finish("Backend unavailable.");
        compare(findChild(view,"logsDeviceReportResult").text,"Backend unavailable.");
        bridge.connected=false;verify(!button.enabled);
    }
    function test_controls_meet_the_touch_size(){
        for(const name of ["logsRefresh","logsCopy","logsExport","logsDeviceReport","logsReset","option_warning","option_error","option_all","logsSource","logsAge","logsComponent","logsSearch"])
            verify(findChild(view,name).height>=48,name+" is "+findChild(view,name).height);
        fake.records=[{time:"2026-10-14T12:00:00Z",event:"a",component:"fan",source:"backend",level:"error"}];
        verify(row(0).height>=48);
    }
}
