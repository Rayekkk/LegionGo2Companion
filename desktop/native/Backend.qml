// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import "updates.mjs" as Updates

QtObject {
    id: backend
    property var transport
    property string page: "overview"
    property bool windowActive: true
    property var snapshots: ({})
    property var received: ({})
    property var requests: ({})
    property bool busy: false
    property string error: ""
    property bool connected: false
    property var readErrors: ({})
    // A refused firmware step (e.g. a pad is detached) stays on screen until the next step; polling must not clear it.
    property string firmwareStepError: ""
    property var host: null
    property int generation: 0
    property string diagnosticToken: ""
    property string cpuApp: ""
    property bool cpuAc: false
    property string gameProfileApp:""
    property string editApp:"" // a saved profile edited on the TDP page instead of the running game's
    property real overviewDetailsAt:-1000
    property real networkDetailsAt:-1000
    property real controllerDetailsAt:-1000
    property bool reportSaving:false // save_device_report runs beside the rest of the app, not as a blocking change
    property string reportText:""
    property real panelLeaseAt:-1000 // backend lease TTL is 90 s; renew every 30 s, not every poll
    signal changed(string method)
    signal mutationDone(string method, var result, bool success)

    function profileApp() { return editApp || String(state("desktop_context").app_id || ""); }
    function state(method) { return snapshots[method] === undefined ? {} : snapshots[method]; }
    function now() { return transport.monotonicSeconds(); }
    function age(method) { return received[method] === undefined ? Infinity : now()-received[method]; }
    function owner(method) {
        if(method==="get_version" || method==="desktop_context" || method.startsWith("modules_") || method.startsWith("updates_") || method.startsWith("firmware_"))return "";
        for(const pair of [["fan_","fan"],["vibe_","vibration"],["rgb_","rgb"],["remap_","remap"],["controller_","controller"],["battery_","battery"],["display_","display"],["wifi_","wifi"]])
            if(method.startsWith(pair[0]))return pair[1];
        return "tdp";
    }
    function canRead(method) {
        const name=owner(method), modules=state("get_version").modules, entry=modules?.[name];
        // A Battery reading that the controller code serves: not asked where the controllers do not apply.
        if(method==="battery_get_controller_levels" && modules?.controller?.applicable===false)return false;
        return !name || !!entry && entry.applicable!==false && entry.enabled!==false && entry.ready===true && !entry.pending && !entry.maintenance;
    }
    function moduleError(name) { for(const method of Object.keys(readErrors))if(owner(method)===name && readErrors[method])return readErrors[method];return ""; }
    // The request id; -1 when nothing was sent (no onFinished follows).
    function issue(method, args, mutation, blocking, poll, then) {
        if (!transport || (host && !host.installed)) return -1;
        const id = transport.call(method, args || [], mutation || false);
        const copy = Object.assign({}, requests);
        copy[id] = {method:method, args:args || [], mutation:!!mutation, blocking:!!blocking, generation:generation, started:now(), poll:!!poll, then:then || null};
        requests = copy;
        return id;
    }
    // then: {method, args} sent only after this mutation succeeds. It lives here, not in a page,
    // so leaving the page cannot drop or retarget the second half of one user choice.
    function mutate(method, args, then) {
        if (busy || !connected || (host && !host.installed)) return;
        busy = true; error = "";
        if(method.startsWith("firmware_")) {
            const times=Object.assign({},received);delete times.firmware_get_status;received=times;
        }
        transport.cancelReads();
        issue(method, args, true, true, false, then);
    }
    function saveDeviceReport() {
        if(reportSaving || !connected || (host && !host.installed))return;
        reportText="";
        reportSaving=issue("save_device_report",[],true,false)>=0;
    }
    function panelMethods() {
        if(page === "controller") {
            let methods=["controller_get_status"].concat(state("get_version")?.device?.firmware===false?[]:["firmware_get_status"]);
            if(now()-controllerDetailsAt>=3) {
                controllerDetailsAt=now();
                methods=methods.concat(["vibe_is_ready","vibe_get_settings","vibe_get_capabilities",
                    "vibe_get_driver_status","remap_get_status","battery_get_controller_levels"]);
            }
            return methods;
        }
        if(page === "overview") {
            // Keep fast sensors fresh without rereading every HID/WMI module
            // every second. Viewing this dashboard only uses existing getters.
            let methods=["fan_get_state"];
            if(now()-overviewDetailsAt>=6) {
                overviewDetailsAt=now();
                methods=methods.concat(["get_settings","get_tdp_info","get_power_source",
                    "battery_get_status","battery_get_controller_levels",
                    "controller_get_status","vibe_get_settings","vibe_get_capabilities"]);
            }
            if(now()-networkDetailsAt>=10) { networkDetailsAt=now();methods.push("wifi_get_status"); }
            return methods;
        }
        const panels = {
            tdp:["get_settings","get_caps","get_extras_unlocked","get_power_source","get_tdp_info","get_cpu_power_controls","list_game_profiles"],
            fan:["fan_get_state"],
            rgb:["rgb_get_status"],
            battery:["battery_get_status","battery_get_controller_levels"],
            display:["display_get_state"], wifi:["wifi_get_status"], modules:[], about:[]
        };
        return panels[page] || [];
    }
    function refresh() {
        if ((!windowActive && !(host && host.trayVisible)) || busy || Object.keys(requests).length || (host && !host.installed)) return;
        // Obtain module gates before admitting any component getters.
        issue("get_version",[],false,false,true);
    }
    function refreshPanel() {
        if(busy || !connected || (host && !host.installed))return;
        const gate=state("get_version");
        if(!gate || typeof gate!=="object" || Array.isArray(gate) || gate.blocked!==false) {
            // The independent update outcome is readable while all hardware
            // controls are paused. Do not admit any component getters here.
            if(windowActive && page==="controller" && gate?.device?.firmware!==false)issue("firmware_get_status");
            return;
        }
        if(!windowActive) { if(host && host.trayVisible)issue("desktop_context");return; }
        issue("desktop_context");
        for (const method of panelMethods()) if(canRead(method)) issue(method,method==="get_cpu_power_controls"?[cpuApp,cpuAc]:[]);
        if(page==="tdp" && profileApp() && canRead("get_game_profile"))issue("get_game_profile",[profileApp()]);
        if(page==="controller" && diagnosticToken && canRead("controller_get_diagnostics"))issue("controller_get_diagnostics",[diagnosticToken]);
        if((page==="overview" || page==="tdp" || page==="controller") && canRead("set_panel_active") && now()-panelLeaseAt>=30) { panelLeaseAt=now();issue("set_panel_active",[true]); }
    }
    function refreshNow() {
        overviewDetailsAt=-1000;
        networkDetailsAt=-1000;
        controllerDetailsAt=-1000;
        refresh();
    }
    function navigate() {
        generation++;
        // Leaving the TDP page can destroy the page that set this, so it is cleared here.
        editApp = "";
        if(page === "overview") { overviewDetailsAt=-1000;networkDetailsAt=-1000; }
        if(page === "controller") controllerDetailsAt=-1000;
        transport.cancelReads();
        if (diagnosticToken && page !== "controller") {
            issue("controller_stop_diagnostics",[diagnosticToken],true);
            diagnosticToken = "";
        }
        if (page !== "overview" && page !== "tdp" && page !== "controller") { panelLeaseAt=-1000;if(canRead("set_panel_active") && connected)issue("set_panel_active",[false],true); }
        if (!Object.keys(requests).length) refresh();
    }
    function shutdown() {
        if (diagnosticToken) {
            issue("controller_stop_diagnostics",[diagnosticToken],true);
            diagnosticToken = "";
        }
        transport.cancelReads(true);
        windowActive = false; // onWindowActiveChanged releases the panel lease once
    }
    property Connections connection: Connections {
        target: backend.transport
        function onFinished(id, method, result, problem) {
            if(method==="save_device_report") { backend.reportSaving=false;backend.reportText=Updates.deviceReportResult(problem?{error:problem}:result); }
            const request = backend.requests[id];
            if (!request) return;
            const copy = Object.assign({},backend.requests); delete copy[id]; backend.requests = copy;
            if (request.blocking) backend.busy = false;
            if(backend.host && !backend.host.installed) {
                backend.connected=false;
                if(request.mutation)backend.mutationDone(method,result,false);
                return;
            }
            // A dropped or failed lease must not block the next poll from re-sending it for 30 s.
            if(method==="set_panel_active" && request.args[0]===true && problem)backend.panelLeaseAt=-1000;
            if (problem === "Obsolete read." || (!request.mutation && request.generation!==backend.generation)) return;
            if(!problem && method==="get_version" && (!result || typeof result!=="object" || Array.isArray(result) || typeof result.blocked!=="boolean"))
                problem="Module status is unavailable. Try again in a moment.";
            if(!problem && method==="desktop_context" && (!result || typeof result!=="object" || Array.isArray(result) || typeof result.app_id!=="string" || typeof result.gamescope!=="boolean"))
                problem="Desktop session status is unavailable. Reconnect before changing settings.";
            const readErrors=Object.assign({},backend.readErrors);
            if(!request.mutation) { readErrors[method]=problem;backend.readErrors=readErrors; }
            // The backend ends a capture itself after 30 s or a 3 s lease gap.
            if (method==="controller_get_diagnostics" && request.args[0]===backend.diagnosticToken
                && (result?.active===false || String(problem).indexOf("no longer available")>=0)) backend.diagnosticToken="";
            if (problem) {
                if(method.startsWith("firmware_")) {
                    if(request.mutation)backend.firmwareStepError=problem;
                    const times=Object.assign({},backend.received);delete times.firmware_get_status;backend.received=times;
                    const errors=Object.assign({},backend.readErrors);errors.firmware_get_status=problem;backend.readErrors=errors;
                }
                if(request.blocking)backend.error = problem;
                if (method === "get_version" || method === "desktop_context") backend.connected = false;
            } else if (request.generation === backend.generation || request.mutation) {
                if (method==="get_game_profile" && request.args[0]!==backend.profileApp()) return;
                if (method==="get_cpu_power_controls" && (String(request.args[0] || "")!==backend.cpuApp || !!request.args[1]!==backend.cpuAc)) return;
                if (method==="get_game_profile") backend.gameProfileApp=request.args[0];
                if (method==="desktop_context" && !backend.editApp && String(result.app_id || "")!==String(backend.state("desktop_context").app_id || "")) backend.gameProfileApp="";
                let key = method;
                if (method.startsWith("fan_")) key = "fan_get_state";
                else if (method.startsWith("firmware_")) key = "firmware_get_status";
                if(method.startsWith("firmware_")) {
                    if(request.mutation)backend.firmwareStepError="";
                    const errors=Object.assign({},backend.readErrors);errors.firmware_get_status="";backend.readErrors=errors;
                }
                else if (method === "battery_set_enabled" || method === "battery_release_control" || method === "battery_reset_charging_mode") key = "battery_get_status";
                // RGB writes return the new status: hue and saturation are sent together,
                // so the next slider must start from it, not from the last poll.
                else if (method.startsWith("rgb_set_") || method==="rgb_restore_original") key = "rgb_get_status";
                if (method === "get_version") {
                    backend.connected = true;
                    // Disabled modules cannot keep stale telemetry or faults on screen.
                    const errors=Object.assign({},backend.readErrors);
                    for(const key of Object.keys(errors)) {
                        const name=backend.owner(key),entry=result.modules?.[name];
                        if(name && (!entry || entry.enabled===false || !entry.ready || entry.pending || entry.maintenance))delete errors[key];
                    }
                    backend.readErrors=errors;
                }
                if (method === "controller_start_diagnostics") backend.diagnosticToken = result.token || "";
                if (method === "controller_stop_diagnostics") backend.diagnosticToken = "";
                const snapshots = Object.assign({},backend.snapshots); snapshots[key] = result;
                backend.snapshots = snapshots;
                const times = Object.assign({},backend.received);
                // Include transport latency: a delayed temperature is not fresh on arrival.
                times[key] = request.started; backend.received = times;
                backend.changed(key);
                if(method==="get_version" && request.poll)Qt.callLater(backend.refreshPanel);
            }
            if (request.mutation) {
                if (request.blocking) backend.controllerDetailsAt=-1000;
                backend.mutationDone(method,result,!problem);
                if (request.then && !problem) backend.mutate(request.then.method, request.then.args);
                Qt.callLater(backend.refresh);
            }
        }
    }
    property Timer polling: Timer {
        interval: !backend.windowActive ? 10000 : !backend.connected ? 3000 : backend.page === "fan" || backend.page === "overview" || (backend.page === "controller" && backend.diagnosticToken) ? 1000 : 3000
        repeat: true
        running: backend.windowActive || !!(backend.host && backend.host.trayVisible)
        onTriggered: backend.refresh()
    }
    onPageChanged: navigate()
    onWindowActiveChanged: {
        panelLeaseAt=-1000;
        if (windowActive) refresh();
        else if (transport) { transport.cancelReads(); if(canRead("set_panel_active") && connected)issue("set_panel_active",[false],true); }
    }
    Component.onCompleted: Qt.callLater(refresh)
}
