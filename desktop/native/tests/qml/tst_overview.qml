// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtTest
import "../.."
TestCase {
    name:"NativeOverview"; when:windowShown; width:1050; height:1200
    QtObject {
        id:fake
        property var data:({})
        property real clock:10
        property bool connected:true
        property bool windowActive:false
        function now() { return clock; }
        function age(unused) { return clock-10; }
        function state(method) { return data[method] || {}; }
        function canRead(method) {
            if(method!=="fan_get_state")return true;
            const entry=data.get_version?.modules?.fan;
            return entry?.enabled!==false && entry?.ready===true;
        }
    }
    Component { id:factory; OverviewPage { width:1000; backend:fake } }
    property var view
    property string target:""
    function init() {
        fake.clock=10;fake.connected=true;target="";
        fake.data={get_version:{blocked:false,modules:{fan:{ready:true},tdp:{ready:true},controller:{ready:true},display:{ready:true}}},
            fan_get_state:{readings:{valid:true,rpm:2400,apu_temperature:55.5,age_seconds:0,automatic:true},settings:{enabled:false,display_unit:"rpm"},limits:{max_rpm:4800}},
            battery_get_status:{capacity:78,charging_status:"Charging",enabled:true},get_power_source:{ac:true},
            get_settings:{active_preset:"balanced"},get_tdp_info:{success:true,values:{spl_limit:20,sppt_limit:25,fppt_limit:30,package_draw:18}},
            wifi_get_status:{connected:true,live:{ssid:"Test network",frequency:"5180",wifi_backend:"iwd"},settings:{band_policy:"off"}},controller_get_status:{available:true,gyro_source:"system"},
            battery_get_controller_levels:{left:80,right:90},vibe_get_settings:{settings:{level:2}},vibe_get_capabilities:{intensity:["off","low","medium","high"]}};
        view=createTemporaryObject(factory,this);verify(view!==null);
        view.navigate.connect(function(page) { target=page; });
    }
    function test_dashboardUsesActualReadbackAndSharedSpeedUnit() {
        compare(findChild(view,"overviewBatteryValue").text,"78%");
        compare(findChild(view,"overviewTemperature").value,"55.5 °C");
        compare(findChild(view,"overviewPower").value,"18.0 W");
        compare(findChild(view,"overviewFan").value,"2400 RPM");
        fake.data=Object.assign({},fake.data,{fan_get_state:Object.assign({},fake.data.fan_get_state,{settings:{display_unit:"percent"}})});
        compare(findChild(view,"overviewFan").value,"50.0%");
    }
    function test_staleAndDisconnectedValuesAreNotShownAsLive() {
        fake.clock=14;view.tick=fake.clock;
        compare(findChild(view,"overviewFan").value,"—");
        fake.clock=23;view.tick=fake.clock;
        compare(findChild(view,"overviewPower").value,"—");
        compare(findChild(view,"overviewBatteryValue").text,"—");
        fake.clock=10;view.tick=10;fake.connected=false;
        compare(findChild(view,"overviewTemperature").value,"—");
    }
    function test_overviewHasNoDuplicateQuickAccess() {
        compare(findChild(view,"overviewShortcut_fan"),null);
        compare(findChild(view,"overviewShortcut_wifi"),null);
        compare(findChild(view,"overviewNetwork").text,"Test network");
        compare(findChild(view,"overviewFrequency").value,"5180 MHz");
        fake.data=Object.assign({},fake.data,{wifi_get_status:{connected:true,live:{frequency:"6295.0"}}});
        compare(findChild(view,"overviewFrequency").value,"6295 MHz");
    }
    function test_missingSamplesNeverInventZeroValues() {
        fake.data={};
        compare(findChild(view,"overviewBatteryValue").text,"—");
        compare(findChild(view,"overviewTemperature").value,"—");
        compare(findChild(view,"overviewPower").value,"—");
        compare(findChild(view,"overviewFrequency").value,"—");
    }
    function test_networkNameIsLiteralTextRatherThanMarkup() {
        const name="<b>Nearby network</b><img src='http://127.0.0.1:1/probe'>";
        fake.data=Object.assign({},fake.data,{wifi_get_status:{connected:true,live:{ssid:name}}});
        const label=findChild(view,"overviewNetwork");
        compare(label.text,name);
        compare(label.textFormat,Text.PlainText);
    }
    function test_powerSampleAgeIncludesItsTimeInTheBackendCache() {
        fake.data=Object.assign({},fake.data,{get_tdp_info:{values:{package_draw:18},sample_age_seconds:11}});
        compare(findChild(view,"overviewPower").value,"18.0 W");
        fake.clock=12;view.tick=fake.clock;
        compare(findChild(view,"overviewPower").value,"—");
    }
    function test_fanControlUsesTheSharedFanHeadline() {
        fake.data=Object.assign({},fake.data,{fan_get_state:Object.assign({supported:true},fake.data.fan_get_state)});
        compare(findChild(view,"overviewFanControl").value,"Automatic");
        const on={supported:true,settings:{enabled:true},active_curve:"global",readings:{valid:false},ownership:{blocked:true,state:"unavailable"}};
        fake.data=Object.assign({},fake.data,{fan_get_state:on});
        compare(findChild(view,"overviewFanControl").value,"Starting your curve…");
        fake.data=Object.assign({},fake.data,{fan_get_state:Object.assign({},on,{telemetry:{active:true}})});
        compare(findChild(view,"overviewFanControl").value,"Global curve");
    }
    function test_powerLimitAndPresetUseTheCanonicalWording() {
        compare(findChild(view,"overviewPowerLimit").value,"SPL 20 · SPPT 25 · FPPT 30 W");
        compare(findChild(view,"overviewPreset").text,"Custom","Unknown limits");
        fake.data=Object.assign({},fake.data,{get_settings:{spl:35000,sppt:37000,fppt:45000,active_preset:"max"}});
        compare(findChild(view,"overviewPreset").text,"Maximum");
        // A game's limits are named by their values, not by Global's saved preset (as in Decky).
        fake.data=Object.assign({},fake.data,{get_settings:{spl:35000,sppt:37000,fppt:45000,active_spl:16000,active_sppt:25000,active_fppt:30000,active_preset:"max"}});
        compare(findChild(view,"overviewPreset").text,"Balanced");
        fake.data=Object.assign({},fake.data,{vibe_get_settings:{settings:{level:2,mode:0}},vibe_get_capabilities:{intensity:["off","low","medium","high"],mode:["fps","racing"]}});
        compare(findChild(view,"overviewVibration").value,"Medium · FPS");
    }
    function test_disabledFanCannotPresentItsLastSampleAsLive() {
        // Clean module disable can retain ready=true in the real backend.
        fake.data=Object.assign({},fake.data,{get_version:{blocked:false,modules:{fan:{enabled:false,ready:true}}}});
        compare(findChild(view,"overviewTemperature").value,"—");
        compare(findChild(view,"overviewFan").value,"—");
    }
}
