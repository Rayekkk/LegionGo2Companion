// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import "fan.mjs" as Fan
ColumnLayout {
    id:overview
    objectName:"deviceOverview"
    required property var backend
    signal navigate(string page)
    UiTheme { id:theme }
    property real tick:backend ? backend.now() : 0
    property var fan:s("fan_get_state")
    property var battery:s("battery_get_status")
    property var power:s("get_settings")
    property var powerInfo:s("get_tdp_info")
    property var network:s("wifi_get_status")
    property var controller:s("controller_get_status")
    property var controllerBattery:s("battery_get_controller_levels")
    property var vibration:s("vibe_get_settings")
    property var modules:s("get_version").modules || {}
    property bool fanFresh:tick>=0 && fresh("fan_get_state",2.5) && Fan.readingFresh(fan.readings,backend.age("fan_get_state"))
    property bool batteryFresh:tick>=0 && fresh("battery_get_status",12)
    property bool performanceFresh:tick>=0 && fresh("get_tdp_info",12) && Number(powerInfo.sample_age_seconds || 0)+backend.age("get_tdp_info")<12
    property bool networkFresh:tick>=0 && fresh("wifi_get_status",20)
    property bool controllerFresh:tick>=0 && fresh("controller_get_status",12)
    spacing:theme.gap
    function s(method) { return backend ? backend.state(method) : {}; }
    function fresh(method,limit) { return !!backend && backend.connected && (!backend.canRead || backend.canRead(method)) && backend.age(method)<limit; }
    function named(value) { const name=String(value || "");return name?name.charAt(0).toUpperCase()+name.slice(1).replace(/_/g," "):"—"; }
    function networkFrequency() { const raw=network.live?.frequency; const value=Number(raw); return raw===null || raw===undefined || raw===""?"—":Number.isFinite(value)?Math.round(value)+" MHz":String(raw); }
    function watts(key) { const value=powerInfo.values?.[key];return value===undefined || value===null?"—":Number(value).toFixed(0); }
    function batteryPercent(value) { return value===null || value===undefined?"—":Math.round(Number(value))+"%"; }
    function vibrationLevel() {
        const settings=vibration.settings || {},caps=s("vibe_get_capabilities");
        if(settings.level===undefined)return "—";
        const mode=(caps.mode || [])[settings.mode];
        return named((caps.intensity || [])[settings.level])+(mode?" · "+(({fps:"FPS",spg:"SPG",rpg:"RPG"})[mode] || named(mode)):"");
    }
    readonly property var gyroLabels:({system:"System default",combined:"Both controllers",left:"Left controller",right:"Right controller"})
    readonly property var bandLabels:({off:"Automatic",five_six_no_24:"Prefer 5/6 GHz",five_six_only:"Only 5/6 GHz",six_ghz_only:"Only 6 GHz"})
    function available(name) { return !backend.connected?"Offline":modules[name]?.pending?"Restoring":modules[name]?.enabled===false?"Off":modules[name]?.error?"Needs attention":modules[name]?.ready?"On":"Unavailable"; }
    Timer { interval:500; repeat:true; running:overview.backend && overview.backend.windowActive; onTriggered:overview.tick=overview.backend.now() }
    GridLayout {
        id:summaryGrid
        Layout.fillWidth:true; columns:overview.width>=860?2:1; columnSpacing:theme.gap; rowSpacing:theme.gap
        UiCard {
            objectName:"overviewBattery"
            padding:20
            Layout.preferredWidth:summaryGrid.columns===2?1:-1; Layout.fillWidth:true; Layout.fillHeight:true; Layout.minimumWidth:0
            title:"Battery"; icon:"battery"
            trailing:UiBadge { text:overview.batteryFresh?overview.battery.charging_status || "Unknown":overview.available("battery"); tone:overview.batteryFresh && overview.battery.charging_status==="Charging"?"green":"blue" }
            Label { textFormat:Text.PlainText; objectName:"overviewBatteryValue"; text:overview.batteryFresh?overview.batteryPercent(overview.battery.capacity):"—"; color:theme.text; font.pixelSize:52; font.weight:Font.DemiBold; Layout.topMargin:6 }
            Rectangle {
                Layout.fillWidth:true; implicitHeight:6; radius:3; color:"#354455"
                Rectangle { width:parent.width*Math.max(0,Math.min(100,Number(overview.battery.capacity || 0)))/100; height:parent.height; radius:3; color:theme.accentText; visible:overview.batteryFresh }
            }
            UiReadout { Layout.fillWidth:true; label:"Power source"; value:fresh("get_power_source",12)?s("get_power_source").ac?"External power":"Battery":"—" }
            UiReadout { Layout.fillWidth:true; label:"Battery protection"; value:overview.batteryFresh?overview.battery.enabled?"On":"Off":"—" }
            Item { Layout.fillHeight:true }
            UiButton { text:"Battery settings"; onClicked:overview.navigate("battery") }
        }
        UiCard {
            objectName:"overviewPerformance"
            padding:20
            Layout.fillWidth:true; Layout.fillHeight:true; Layout.preferredWidth:2; Layout.minimumWidth:0
            title:"Performance & cooling"; icon:"tdp"
            trailing:UiBadge { objectName:"overviewPreset"; text:fresh("get_settings",12)?(overview.power.active_preset || overview.power.preset)==="max"?"Maximum":named(overview.power.active_preset || overview.power.preset):overview.available("tdp") }
            RowLayout { Layout.fillWidth:true; spacing:18; Layout.topMargin:10
                UiMetric { objectName:"overviewTemperature"; Layout.fillWidth:true; Layout.preferredWidth:1; label:"APU temperature"; value:overview.fanFresh?Number(overview.fan.readings.apu_temperature).toFixed(1)+" °C":"—"; note:"Hotter of CPU and GPU" }
                UiMetric { objectName:"overviewPower"; Layout.fillWidth:true; Layout.preferredWidth:1; label:"APU power"; value:overview.performanceFresh && overview.powerInfo.values?.package_draw!==undefined?Number(overview.powerInfo.values.package_draw).toFixed(1)+" W":"—" }
                UiMetric { objectName:"overviewFan"; Layout.fillWidth:true; Layout.preferredWidth:1; label:"Fan speed"; value:overview.fanFresh?Fan.speedText(overview.fan.readings.rpm,overview.fan.settings?.display_unit,overview.fan.limits?.max_rpm):"—" }
            }
            Rectangle { Layout.fillWidth:true; implicitHeight:1; color:theme.divider; Layout.topMargin:6 }
            UiReadout { objectName:"overviewPowerLimit"; Layout.fillWidth:true; label:"Power limit"; value:overview.performanceFresh && overview.watts("spl_limit")!=="—"?overview.watts("spl_limit")+" W (boost "+overview.watts("sppt_limit")+"/"+overview.watts("fppt_limit")+" W)":"—" }
            UiReadout { objectName:"overviewFanControl"; Layout.fillWidth:true; label:"Fan control"; value:fresh("fan_get_state",3)?Fan.fanStatus(overview.fan).headline:"—" }
            Item { Layout.fillHeight:true }
            RowLayout { Layout.fillWidth:true; spacing:10
                UiButton { text:"Power limits"; onClicked:overview.navigate("tdp") }
                UiButton { text:"Fan Control"; onClicked:overview.navigate("fan") }
                Item { Layout.fillWidth:true }
            }
        }
    }
    RowLayout { Layout.fillWidth:true; Layout.topMargin:4
        Label { textFormat:Text.PlainText; text:"Current setup"; color:theme.text; font.pixelSize:17; font.weight:Font.DemiBold; Layout.fillWidth:true }
    }
    GridLayout { Layout.fillWidth:true; columns:overview.width>=700?2:1; columnSpacing:theme.gap; rowSpacing:theme.gap
        UiCard {
            padding:20
            Layout.fillWidth:true; Layout.fillHeight:true; Layout.preferredWidth:1; Layout.minimumWidth:0
            title:"Wi-Fi"; icon:"wifi"
            trailing:UiBadge { text:overview.networkFresh?overview.network.connected?"Connected":"Disconnected":overview.available("wifi"); tone:overview.networkFresh && overview.network.connected?"green":"amber" }
            Label { textFormat:Text.PlainText; objectName:"overviewNetwork"; text:overview.networkFresh?overview.network.connected?overview.network.live?.ssid || "Connected network":"No connection":"—"; color:theme.text; font.pixelSize:18; font.weight:Font.DemiBold; Layout.fillWidth:true; elide:Text.ElideRight }
            UiReadout { objectName:"overviewFrequency"; Layout.fillWidth:true; label:"Frequency"; value:overview.networkFresh && overview.network.connected?overview.networkFrequency():"—" }
            UiReadout { Layout.fillWidth:true; label:"Wi-Fi band"; value:overview.networkFresh?overview.bandLabels[overview.network.settings?.band_policy || "off"] || named(overview.network.settings?.band_policy):"—" }
            Item { Layout.fillHeight:true }
            UiButton { text:"Wi-Fi settings"; onClicked:overview.navigate("wifi") }
        }
        UiCard {
            padding:20
            Layout.fillWidth:true; Layout.fillHeight:true; Layout.preferredWidth:1; Layout.minimumWidth:0
            title:"Controllers"; icon:"controller"
            trailing:UiBadge { visible:!modules.controller?.ready; text:"Gyro: "+overview.available("controller"); tone:"amber" }
            Label { textFormat:Text.PlainText; visible:overview.controllerFresh && !overview.controller.available; text:"Controller unavailable"; color:theme.warning; font.pixelSize:14; Layout.fillWidth:true }
            UiReadout { Layout.fillWidth:true; label:"Left / right battery"; value:fresh("battery_get_controller_levels",12)?[overview.batteryPercent(overview.controllerBattery.left),overview.batteryPercent(overview.controllerBattery.right)].join(" / "):"—" }
            UiReadout { objectName:"overviewVibration"; Layout.fillWidth:true; label:"Vibration"; value:fresh("vibe_get_settings",12)?overview.vibrationLevel():overview.available("vibration") }
            UiReadout { Layout.fillWidth:true; label:"Gyro source"; value:overview.controllerFresh?overview.gyroLabels[overview.controller.gyro_source] || named(overview.controller.gyro_source):overview.available("controller") }
            Item { Layout.fillHeight:true }
            UiButton { text:"Controllers"; onClicked:overview.navigate("controller") }
        }
    }
}
