// SPDX-License-Identifier: BSD-3-Clause
import QtQuick
import QtTest
import "../.."
TestCase {
    name:"ControllerMaintenanceStatus";when:windowShown;width:600;height:300
    QtObject {
        id:fake
        property bool connected:true
        property real elapsed:0
        property var data:({})
        function state(method) { return method==="get_version"?data:{}; }
        function age(method) { return elapsed; }
        function now() { return 10; }
    }
    Component { id:factory;ModuleStatus { backend:fake;owner:"controller";tick:10 } }
    function init() { fake.connected=true;fake.elapsed=0;fake.data={blocked:false,modules:{controller:{enabled:true,ready:true}}}; }
    function test_maintenancePausesControlsButDoesNotDisableSavedChoice() {
        let status=createTemporaryObject(factory,this);verify(status!==null);
        compare(status.state,"ready");
        fake.data={blocked:false,modules:{controller:{enabled:true,ready:false,maintenance:true,note:"Controller operations are paused for firmware maintenance."}}};
        compare(status.state,"maintenance");compare(status.label,"Paused");verify(!status.ready);
        verify(status.entry.enabled);verify(status.message.indexOf("firmware maintenance")>=0);
        fake.data={blocked:false,modules:{controller:{enabled:true,ready:true}}};
        compare(status.state,"ready");verify(status.ready);
    }
    function test_staleOrOfflineMaintenanceCannotBePresentedAsFresh() {
        fake.data={blocked:false,modules:{controller:{enabled:true,ready:false,maintenance:true}}};
        let status=createTemporaryObject(factory,this);compare(status.state,"maintenance");
        fake.elapsed=9;compare(status.state,"stale");verify(!status.ready);
        fake.connected=false;compare(status.state,"offline");verify(!status.ready);
    }
    function test_moduleOffIsStillSeparateFromMaintenance() {
        fake.data={blocked:false,modules:{controller:{enabled:false,ready:true}}};
        let status=createTemporaryObject(factory,this);compare(status.state,"disabled");verify(!status.ready);
        verify(status.message.indexOf("Modules")>=0);
    }
}
