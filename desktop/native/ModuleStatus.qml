// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
QtObject {
    id:status
    required property var backend
    property string owner:""
    property real tick:backend ? backend.now() : 0
    readonly property var entry:backend ? backend.state("get_version")?.modules?.[owner] || {} : {}
    readonly property var gate:backend ? backend.state("get_version") : null
    readonly property bool validGate:!!gate && typeof gate==="object" && !Array.isArray(gate) && typeof gate.blocked==="boolean"
    readonly property bool fresh:tick>=0 && !!backend && validGate && backend.connected && backend.age("get_version")<8
    readonly property string state:!backend || !backend.connected?"offline":!fresh?"stale":gate.blocked?"blocked":entry.maintenance?"maintenance":entry.pending?"pending":entry.enabled===false?"disabled":entry.ready?"ready":entry.error?"fault":"loading"
    readonly property bool ready:state==="ready"
    readonly property string label:({offline:"Offline",stale:"Waiting for status",blocked:"Paused",maintenance:"Paused",pending:"Restoring",disabled:"Off",fault:"Unavailable",loading:"Loading",ready:""})[state]
    readonly property string message:state==="maintenance"?entry.note || "Controller operations are paused for firmware maintenance.":state==="offline"?"Waiting for a connection to Companion.":state==="stale"?"Waiting for a fresh module status. Controls are paused.":state==="disabled"?"This feature is turned off in Modules.":state==="pending"?entry.error || "Restoration is pending. Open Modules to review this feature.":state==="fault"?entry.error || "This feature could not start.":state==="blocked"?backend.state("get_version").message || "Companion is paused.":state==="loading"?"Waiting for this feature to start.":""
}
