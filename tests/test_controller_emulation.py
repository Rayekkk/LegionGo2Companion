# SPDX-License-Identifier: BSD-3-Clause

import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from test_controller_backend import MemorySettings

import controller_backend as backend
import controller_emulation as emulation


DEVICE = {
    "service_owner": ":1.42",
    "path": "/org/shadowblip/InputPlumber/CompositeDevice0",
    "generation": "legion|:1.42",
}
BASE = ["deck-uhid", "mouse", "keyboard", "touchpad"]


class TargetTests(unittest.TestCase):
    def test_queries_only_the_discovered_service_and_reads_scalar_target_types(self):
        path = "/org/shadowblip/InputPlumber/devices/target/gamepad0"
        with patch.object(emulation, "json_value", side_effect=[[path], "deck-uhid"]) as dbus:
            self.assertEqual(emulation.targets(DEVICE), ["deck-uhid"])
        self.assertEqual(dbus.call_args_list[1].args[0],
                         ["get-property", ":1.42", path, emulation.TARGET_INTERFACE, "DeviceType"])

    def test_switch_replaces_only_the_gamepad_and_checks_readback(self):
        expected = ["xbox-elite", "mouse", "keyboard", "touchpad"]
        with (patch.object(emulation, "targets", side_effect=[BASE, expected]),
              patch.object(emulation, "run_busctl") as dbus):
            emulation.switch(DEVICE, BASE, "xbox-elite")
        dbus.assert_called_once_with(["call", ":1.42", DEVICE["path"], emulation.INTERFACE,
                                     "SetTargetDevices", "as", "4", *expected], timeout=2.0)

    def test_failed_attachment_restores_the_previous_gamepad_and_auxiliary_targets(self):
        with (patch.object(emulation, "targets", side_effect=[BASE, BASE[1:]]),
              patch.object(emulation, "_wait_for", side_effect=[False, True]),
              patch.object(emulation, "_set_targets") as write):
            with self.assertRaisesRegex(emulation.InputPlumberError, "previous mode is active"):
                emulation.switch(DEVICE, BASE, "ds5")
        self.assertEqual(write.call_args_list[0].args, (DEVICE, ["ds5", *BASE[1:]]))
        self.assertEqual(write.call_args_list[1].args, (DEVICE, BASE))

    def test_ambiguous_or_external_targets_are_left_alone(self):
        with patch.object(emulation, "_set_targets") as write:
            with self.assertRaises(emulation.InputPlumberError):
                emulation.switch(DEVICE, [*BASE, "xbox-elite"], "ds5")
            with patch.object(emulation, "targets", return_value=["ds5", *BASE[1:]]):
                with self.assertRaisesRegex(emulation.InputPlumberError, "targets changed"):
                    emulation.switch(DEVICE, BASE, "xbox-elite")
        write.assert_not_called()

    def test_missing_gamepad_is_recreated_with_confirmed_auxiliaries(self):
        current = ["keyboard"]
        expected = ["xbox-elite", *BASE[1:]]
        with (patch.object(emulation, "targets", side_effect=[current, expected]),
              patch.object(emulation, "_set_targets") as write):
            emulation.recover(DEVICE, current, BASE, "xbox-elite", "deck-uhid")
        self.assertEqual(write.call_args.args[0], DEVICE)
        self.assertCountEqual(write.call_args.args[1], expected)

    def test_failed_missing_gamepad_recovery_leaves_a_steam_target(self):
        current = list(BASE[1:])
        with (patch.object(emulation, "targets", return_value=current),
              patch.object(emulation, "_wait_for", side_effect=[False, True]),
              patch.object(emulation, "_set_targets") as write):
            with self.assertRaisesRegex(emulation.InputPlumberError, "previous mode is active"):
                emulation.recover(DEVICE, current, BASE, "xbox-elite", "deck-uhid")
        self.assertCountEqual(write.call_args.args[1], BASE)


class EmulationPreferenceTests(unittest.TestCase):
    def setUp(self):
        self.plugin = backend.Plugin()
        self.state = MemorySettings()
        self.live = list(BASE)
        self.start_patch(backend, "settings", self.state)
        self.start_patch(backend, "_console", return_value="go2")
        self.start_patch(backend, "_hid_inventory", return_value=({"path": "/dev/hidraw0"}, None))
        self.start_patch(self.plugin, "_emulation_device", return_value=DEVICE)
        self.start_patch(emulation, "modes", return_value=emulation.MODES)
        self.start_patch(emulation, "targets", side_effect=lambda device: self.live.copy())
        self.start_patch(emulation, "switch", side_effect=self.switch)

    def start_patch(self, target, key, *args, **kwargs):
        mock = patch.object(target, key, *args, **kwargs)
        self.addCleanup(mock.stop)
        return mock.start()

    def switch(self, device, current, target):
        self.live = [target if kind == emulation.gamepad(current) else kind for kind in current]

    def test_selections_keep_the_original_target_and_system_restores_it(self):
        self.plugin._set_emulation("xbox-elite")
        self.plugin._set_emulation("ds5")
        self.assertEqual(self.state.committed["emulation"], {"baseline": "deck-uhid", "target": "ds5"})
        self.assertEqual(self.plugin._emulation_status(None, DEVICE)["active"], "ds5")
        self.plugin._set_emulation("system")
        self.assertEqual(self.live, BASE)
        self.assertIsNone(self.state.committed["emulation"])

    def test_choice_is_restored_on_a_reset_without_overwriting_an_external_mode(self):
        self.plugin._set_emulation("ds5")
        self.plugin._sync_emulation(release=True)
        self.assertEqual(self.live, BASE)
        self.plugin._sync_emulation(now=5)
        self.assertEqual(self.live, BASE)
        self.plugin._sync_emulation(now=10)
        self.assertEqual(self.live[0], "ds5")
        self.live = ["xbox-elite", *BASE[1:]]
        self.plugin._sync_emulation(now=75)
        self.plugin._sync_emulation(now=80)
        self.assertEqual(self.live[0], "xbox-elite")

    def test_resume_reapplies_saved_mode_on_the_same_composite(self):
        self.plugin._set_emulation("xbox-elite")
        self.live = BASE.copy()
        self.plugin._emulation_retry_at = 100
        self.plugin._sync_emulation(now=15, resumed=True)
        self.assertEqual(self.live, BASE)
        self.plugin._sync_emulation(now=20)
        self.assertEqual(self.live[0], "xbox-elite")
        self.assertEqual(self.state.committed["emulation"]["target"], "xbox-elite")

    def test_gamepad_temporarily_missing_after_exiting_a_game(self):
        self.plugin._set_emulation("xbox-elite")
        self.live = BASE[1:]
        with patch.object(emulation, "recover") as recover:
            self.plugin._sync_emulation(now=5)
            recover.assert_not_called()
            self.plugin._sync_emulation(now=10)
            recover.assert_called_once_with(DEVICE, BASE[1:], ["xbox-elite", *BASE[1:]],
                                            "xbox-elite", "deck-uhid")

    def test_reset_cannot_cause_a_switch_every_poll(self):
        self.plugin._set_emulation("xbox-elite")
        self.live = BASE.copy()
        self.plugin._sync_emulation(now=5)
        self.plugin._sync_emulation(now=10)
        self.assertEqual(self.live[0], "xbox-elite")
        self.live = BASE.copy()
        self.plugin._sync_emulation(now=15)
        self.plugin._sync_emulation(now=20)
        self.assertEqual(self.live, BASE)
        self.assertEqual(self.state.committed["emulation"]["target"], "xbox-elite")

    def test_watch_waits_for_resume_to_settle_and_checks_again(self):
        self.plugin._set_emulation("xbox-elite")
        clock = [0.0]
        live_after_tick = {}

        async def sleep(seconds):
            live_after_tick[clock[0]] = self.live[0]
            clock[0] += seconds
            if clock[0] == 5:
                self.live = BASE.copy()
            if clock[0] > 25:
                raise asyncio.CancelledError

        with (patch.object(backend, "time", types.SimpleNamespace(monotonic=lambda: clock[0])),
              patch.object(backend.asyncio, "sleep", side_effect=sleep),
              patch.object(backend.module_runtime, "suspend_offset", side_effect=lambda: 10 if clock[0] else 0),
              patch.object(self.plugin, "_sleep_check", new_callable=AsyncMock),
              patch.object(self.plugin, "_watch_check", new_callable=AsyncMock)):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(self.plugin._watch())
        self.assertEqual(live_after_tick[15], "deck-uhid")
        self.assertEqual(live_after_tick[20], "deck-uhid")
        self.assertEqual(live_after_tick[25], "xbox-elite")

    def test_switch_during_diagnostics_does_not_change_the_gamepad(self):
        self.plugin._capture = {"active": True}
        with self.assertRaisesRegex(backend.ControllerError, "Stop the controller test"):
            self.plugin._set_emulation("ds5")
        self.assertEqual(self.live, BASE)
        self.assertIsNone(backend._emulation_choice())
