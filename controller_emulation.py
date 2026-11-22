# SPDX-License-Identifier: BSD-3-Clause
"""Change the gamepad target of an existing InputPlumber composite device."""

from __future__ import annotations

import re
import time

from inputplumber_process import INTERFACE, InputPlumberError, json_value, run_busctl

MODES = {"steam": "deck-uhid", "xbox-elite": "xbox-elite", "ds5": "ds5"}
GAMEPADS = {"deck": "steam", **{target: mode for mode, target in MODES.items()}}
AUXILIARY = {"keyboard", "mouse", "touchpad", "touchscreen", "dbus", "debug", "null"}
MANAGER = "/org/shadowblip/InputPlumber/Manager"
MANAGER_INTERFACE = "org.shadowblip.InputManager"
TARGET_INTERFACE = "org.shadowblip.Input.Target"
TARGET_PATH = re.compile(r"/org/shadowblip/InputPlumber/devices/target/[A-Za-z0-9_]+")
OWNER = re.compile(r":[0-9]+\.[0-9]+")


def _strings(value):
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise InputPlumberError("InputPlumber returned invalid controller targets.")
    return value


def targets(device: dict) -> list[str]:
    owner = device["service_owner"]
    if not OWNER.fullmatch(owner):
        raise InputPlumberError("InputPlumber service identity could not be verified.")
    paths = _strings(json_value(["get-property", owner, device["path"], INTERFACE, "TargetDevices"]))
    kinds = []
    for path in paths:
        if not TARGET_PATH.fullmatch(path):
            raise InputPlumberError("InputPlumber returned an unknown target path.")
        kind = json_value(["get-property", owner, path, TARGET_INTERFACE, "DeviceType"])
        if not isinstance(kind, str):
            raise InputPlumberError("InputPlumber returned an unknown target type.")
        kinds.append(kind)
    return kinds


def gamepad(kinds: list[str]) -> str:
    pads = [kind for kind in kinds if kind in GAMEPADS]
    if len(pads) != 1 or set(kinds) - GAMEPADS.keys() - AUXILIARY:
        raise InputPlumberError("InputPlumber must have one supported gamepad target.")
    return pads[0]


def modes(device: dict) -> dict[str, str]:
    supported = _strings(json_value(["get-property", device["service_owner"], MANAGER,
                                     MANAGER_INTERFACE, "SupportedTargetDeviceIds"]))
    choices = {mode: target for mode, target in MODES.items() if target in supported}
    if "steam" not in choices and "deck" in supported:
        choices["steam"] = "deck"
    return choices


def _set_targets(device: dict, kinds: list[str]) -> None:
    run_busctl(["call", device["service_owner"], device["path"], INTERFACE,
                "SetTargetDevices", "as", str(len(kinds)), *kinds], timeout=2.0)


def _wait_for(device: dict, expected: list[str]) -> bool:
    # SetTargetDevices queues the change. The gamepad and its D-Bus interface appear later.
    until = time.monotonic() + 2.0
    while True:
        try:
            if sorted(targets(device)) == sorted(expected):
                return True
        except InputPlumberError:
            pass
        if time.monotonic() >= until:
            return False
        time.sleep(0.10)


def switch(device: dict, current: list[str], target: str) -> None:
    previous = gamepad(current)
    if previous == target:
        return
    if sorted(targets(device)) != sorted(current):
        raise InputPlumberError("Controller targets changed. Try again.")
    expected = [target if kind == previous else kind for kind in current]
    try:
        _set_targets(device, expected)
    except InputPlumberError:
        # A D-Bus timeout can occur after the command is queued. Check the actual result.
        pass
    if _wait_for(device, expected):
        return
    # Restore only this composite, and only while it contains our targets or a subset
    # left by the switch. A different target means another client intervened.
    restored = False
    try:
        actual = targets(device)
        restored = sorted(actual) == sorted(current)
        if not restored and set(actual) <= set(current + expected):
            _set_targets(device, current)
            restored = _wait_for(device, current)
    except InputPlumberError:
        pass
    if restored:
        raise InputPlumberError("InputPlumber could not switch modes. The previous mode is active.")
    raise InputPlumberError("InputPlumber could not confirm the switch or restore the previous mode.")
