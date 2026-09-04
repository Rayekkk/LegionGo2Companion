# SPDX-License-Identifier: BSD-3-Clause
# Copyright (c) 2026 Rayekkk
"""Safe remapping for the Legion Go 2 Desktop and Page buttons.

The controller is already exclusively owned by SteamOS' InputPlumber service.
Companion therefore uses InputPlumber's documented D-Bus profile API instead
of racing it for evdev/hidraw access.  Only the two dedicated button mappings
are replaced; every other mapping in the active profile is retained verbatim.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import re
import subprocess
import threading
import time
from typing import Any

import decky

from safe_settings import SettingsManager


BUSCTL = "/usr/bin/busctl"
SERVICE = "org.shadowblip.InputPlumber"
INTERFACE = "org.shadowblip.Input.CompositeDevice"
DEVICE_PATH_RE = re.compile(
    r"^/org/shadowblip/InputPlumber/CompositeDevice[0-9]+$"
)
EXPECTED_NAMES = {"Lenovo Legion Go 2"}
EXPECTED_PRODUCTS = {"83N0", "83N1"}
EXPECTED_VENDOR = "LENOVO"
MAX_PROFILE_BYTES = 128 * 1024
PROFILE_NAME = "Legion Go 2 Companion Buttons"
CHECK_INTERVAL_S = 60.0
RESUME_CHECK_S = 5.0

BUTTON_SOURCES = {
    "desktop": "Keyboard",
    "page": "QuickAccess2",
}
DEFAULT_ACTIONS = {
    "desktop": "default",
    "page": "default",
}
ACTION_LABELS = {
    "default": "Default",
    "keyboard": "On-screen keyboard",
    "screenshot": "Screenshot",
    "steam": "Steam menu",
    "quick_access": "Quick access menu",
    "show_desktop": "Show desktop",
    "alt_tab": "Switch window (Alt+Tab)",
    "escape": "Escape",
    "enter": "Enter",
    "page_up": "Page Up",
    "page_down": "Page Down",
    "home": "Home",
    "end": "End",
    "f1": "F1",
    "f2": "F2",
    "f3": "F3",
    "f4": "F4",
    "f5": "F5",
    "f6": "F6",
    "f7": "F7",
    "f8": "F8",
    "f9": "F9",
    "f10": "F10",
    "f11": "F11",
    "f12": "F12",
    "disabled": "Disabled",
}

SCHEMA_VERSION = 1
DEFAULT_STATE: dict[str, Any] = {
    "schema_version": SCHEMA_VERSION,
    "enabled": False,
    "desktop_action": "default",
    "page_action": "default",
    "baseline_profile": None,
}

settings = SettingsManager(
    name="remap_settings",
    settings_directory=decky.DECKY_PLUGIN_SETTINGS_DIR,
)

_state_lock = threading.RLock()
_operation_lock = threading.RLock()
_watch_task: asyncio.Task | None = None
_last_error = ""
_last_suspend_offset: float | None = None


class RemapError(RuntimeError):
    pass


def _valid_profile(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if len(value.encode("utf-8")) > MAX_PROFILE_BYTES:
        return False
    return bool(
        re.search(r"(?m)^version:\s*1\s*$", value)
        and re.search(r"(?m)^kind:\s*DeviceProfile\s*$", value)
        and re.search(r"(?m)^mapping:\s*$", value)
    )


def _sanitize_state(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    state = copy.deepcopy(DEFAULT_STATE)
    state["enabled"] = source.get("enabled") is True
    for button in BUTTON_SOURCES:
        key = f"{button}_action"
        value = source.get(key)
        state[key] = value if isinstance(value, str) and value in ACTION_LABELS else DEFAULT_ACTIONS[button]
    baseline = source.get("baseline_profile")
    state["baseline_profile"] = baseline if _valid_profile(baseline) else None
    # An enabled state without its exact restoration profile is unsafe.  Do not
    # invent a baseline and later overwrite a user's InputPlumber profile.
    if state["enabled"] and state["baseline_profile"] is None:
        state["enabled"] = False
    return state


def _load_state() -> dict[str, Any]:
    with _state_lock:
        return _sanitize_state(settings.getSetting("state", DEFAULT_STATE))


def _save_state(state: dict[str, Any]) -> None:
    sanitized = _sanitize_state(state)
    with _state_lock:
        settings.setSetting("state", sanitized)
        settings.commit()


def _run_busctl(args: list[str], *, timeout: float = 5.0) -> str:
    if not os.path.isfile(BUSCTL):
        raise RemapError("InputPlumber control interface is unavailable.")
    try:
        result = subprocess.run(
            [BUSCTL, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env={**os.environ, "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RemapError(f"InputPlumber did not respond: {exc}") from exc
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise RemapError(message[:500] or "InputPlumber rejected the request.")
    return result.stdout


def _json_value(args: list[str]) -> Any:
    raw = _run_busctl(["--json=short", *args])
    try:
        payload = json.loads(raw)
        return payload["data"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise RemapError("InputPlumber returned an invalid D-Bus response.") from exc


def _get_property(path: str, name: str) -> Any:
    return _json_value([
        "get-property", SERVICE, path, INTERFACE, name,
    ])


def _find_device() -> tuple[str, str]:
    identity = {}
    for name in ("sys_vendor", "product_name"):
        try:
            with open(f"/sys/class/dmi/id/{name}", encoding="ascii") as handle:
                identity[name] = handle.read(128).strip()
        except (OSError, UnicodeError):
            identity[name] = ""
    if (
        identity.get("sys_vendor") != EXPECTED_VENDOR
        or identity.get("product_name") not in EXPECTED_PRODUCTS
    ):
        raise RemapError("Button remapping is limited to the Lenovo Legion Go 2.")

    tree = _run_busctl(["tree", SERVICE, "--list", "--no-pager"])
    candidates = sorted({
        line.strip() for line in tree.splitlines()
        if DEVICE_PATH_RE.fullmatch(line.strip())
    })
    for path in candidates:
        try:
            name = _get_property(path, "Name")
            capabilities = _get_property(path, "Capabilities")
        except RemapError:
            continue
        if name not in EXPECTED_NAMES or not isinstance(capabilities, list):
            continue
        required = {
            "Gamepad:Button:Keyboard",
            "Gamepad:Button:QuickAccess2",
        }
        if required.issubset(set(capabilities)):
            return path, str(name)
    raise RemapError("InputPlumber is not managing a compatible Go 2 controller.")


def _get_profile(path: str) -> str:
    value = _json_value([
        "call", SERVICE, path, INTERFACE, "GetProfileYaml",
    ])
    if not isinstance(value, list) or len(value) != 1 or not _valid_profile(value[0]):
        raise RemapError("The active InputPlumber profile is invalid or unsupported.")
    return value[0]


def _profile_name(profile: str) -> str:
    match = re.search(r"(?m)^name:\s*(.*?)\s*$", profile)
    return match.group(1).strip(" '\"") if match else ""


def _mapping_blocks(profile: str) -> tuple[str, list[str]]:
    if not _valid_profile(profile):
        raise RemapError("Cannot safely edit this InputPlumber profile.")
    lines = profile.splitlines(keepends=True)
    mapping_indexes = [i for i, line in enumerate(lines) if line.rstrip("\r\n") == "mapping:"]
    if len(mapping_indexes) != 1:
        raise RemapError("Unexpected InputPlumber profile layout.")
    start = mapping_indexes[0]
    header = "".join(lines[: start + 1])
    body = lines[start + 1 :]
    starts = [i for i, line in enumerate(body) if line.startswith("- name:")]
    if body and not starts:
        raise RemapError("InputPlumber profile mappings could not be identified.")
    blocks = []
    for index, block_start in enumerate(starts):
        block_end = starts[index + 1] if index + 1 < len(starts) else len(body)
        blocks.append("".join(body[block_start:block_end]))
    return header, blocks


def _source_for_block(block: str) -> str | None:
    match = re.search(
        r"(?m)^  source_event:\r?\n"
        r"    gamepad:\r?\n"
        r"      button:\s*(Keyboard|QuickAccess2)\s*$",
        block,
    )
    return match.group(1) if match else None


def _action_events(button: str, action: str) -> list[tuple[str, str]]:
    if button not in BUTTON_SOURCES or action not in ACTION_LABELS:
        raise ValueError("unsupported button action")
    if action == "default":
        action = "keyboard" if button == "desktop" else "screenshot"
    if re.fullmatch(r"f(?:[1-9]|1[0-2])", action):
        return [("keyboard", f"Key{action.upper()}")]
    events: dict[str, list[tuple[str, str]]] = {
        "keyboard": [("gamepad", "Guide"), ("gamepad", "North")],
        "screenshot": [("gamepad", "Screenshot")],
        "steam": [("gamepad", "Guide")],
        "quick_access": [("gamepad", "QuickAccess")],
        "show_desktop": [("keyboard", "KeyLeftMeta"), ("keyboard", "KeyD")],
        "alt_tab": [("keyboard", "KeyLeftAlt"), ("keyboard", "KeyTab")],
        "escape": [("keyboard", "KeyEsc")],
        "enter": [("keyboard", "KeyEnter")],
        "page_up": [("keyboard", "KeyPageUp")],
        "page_down": [("keyboard", "KeyPageDown")],
        "home": [("keyboard", "KeyHome")],
        "end": [("keyboard", "KeyEnd")],
        "disabled": [],
    }
    return events[action]


def _render_mapping(button: str, action: str) -> str:
    source = BUTTON_SOURCES[button]
    label = "Desktop" if button == "desktop" else "Page"
    lines = [
        f"- name: Companion {label}\n",
        "  source_event:\n",
        "    gamepad:\n",
        f"      button: {source}\n",
        "  target_events:\n",
    ]
    for kind, value in _action_events(button, action):
        if kind == "gamepad":
            lines.extend(("  - gamepad:\n", f"      button: {value}\n"))
        else:
            lines.append(f"  - keyboard: {value}\n")
    return "".join(lines)


def _events_for_block(block: str) -> list[tuple[str, str]] | None:
    match = re.search(r"(?m)^  target_events:(?:\s*\[\])?\s*$", block)
    if not match:
        return None
    tail = block[match.end() :]
    events: list[tuple[str, str]] = []
    lines = tail.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        gamepad = re.fullmatch(r"  - gamepad:\s*", line)
        keyboard = re.fullmatch(r"  - keyboard:\s*(\S+)\s*", line)
        if gamepad:
            if index + 1 >= len(lines):
                return None
            button = re.fullmatch(r"      button:\s*(\S+)\s*", lines[index + 1])
            if not button:
                return None
            events.append(("gamepad", button.group(1)))
            index += 2
            continue
        if keyboard:
            events.append(("keyboard", keyboard.group(1)))
            index += 1
            continue
        if line.strip():
            return None
        index += 1
    return events


def _build_profile(base_profile: str, state: dict[str, Any]) -> str:
    header, blocks = _mapping_blocks(base_profile)
    by_source: dict[str, int] = {}
    for block in blocks:
        source = _source_for_block(block)
        if source:
            by_source[source] = by_source.get(source, 0) + 1
    duplicates = [source for source, count in by_source.items() if count > 1]
    if duplicates:
        raise RemapError("The active profile contains ambiguous dedicated-button mappings.")

    replacements = {
        BUTTON_SOURCES[button]: _render_mapping(button, state[f"{button}_action"])
        for button in BUTTON_SOURCES
    }
    output_blocks = []
    replaced: set[str] = set()
    for block in blocks:
        source = _source_for_block(block)
        if source in replacements:
            output_blocks.append(replacements[source])
            replaced.add(source)
        else:
            output_blocks.append(block)
    for button, source in BUTTON_SOURCES.items():
        if source not in replaced:
            output_blocks.append(_render_mapping(button, state[f"{button}_action"]))

    if not header.endswith("\n"):
        header += "\n"
    header = re.sub(
        r"(?m)^name:\s*.*$",
        f"name: {PROFILE_NAME}",
        header,
        count=1,
    )
    result = header + "".join(output_blocks)
    if len(result.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise RemapError("The resulting InputPlumber profile is too large.")
    return result


def _profile_matches(profile: str, state: dict[str, Any]) -> bool:
    if _profile_name(profile) != PROFILE_NAME:
        return False
    try:
        _, blocks = _mapping_blocks(profile)
    except RemapError:
        return False
    found: dict[str, str] = {}
    for block in blocks:
        source = _source_for_block(block)
        if source:
            if source in found:
                return False
            found[source] = block
    for button, source in BUTTON_SOURCES.items():
        block = found.get(source)
        if block is None or _events_for_block(block) != _action_events(
            button, state[f"{button}_action"]
        ):
            return False
    return True


def _load_profile(path: str, profile: str) -> str:
    if not _valid_profile(profile):
        raise RemapError("Refusing to load an invalid InputPlumber profile.")
    _run_busctl([
        "call", SERVICE, path, INTERFACE, "LoadProfileFromYaml", "s", profile,
    ], timeout=8.0)
    return _get_profile(path)


def _apply_state(state: dict[str, Any], *, adopt_external: bool) -> tuple[dict[str, Any], str]:
    path, _ = _find_device()
    current = _get_profile(path)
    working = copy.deepcopy(state)
    if _profile_name(current) != PROFILE_NAME:
        if adopt_external:
            working["baseline_profile"] = current
            _save_state(working)
        elif not _valid_profile(working.get("baseline_profile")):
            raise RemapError("The original InputPlumber profile is unavailable.")
    base = working.get("baseline_profile")
    if not _valid_profile(base):
        raise RemapError("The original InputPlumber profile is unavailable.")
    if _profile_matches(current, working):
        return working, current
    # Merge our two mappings into the live profile. Other controls may have
    # changed since the baseline was captured.
    desired = _build_profile(current, working)
    applied = _load_profile(path, desired)
    if not _profile_matches(applied, working):
        raise RemapError("InputPlumber did not confirm both requested mappings.")
    return working, applied


def _restore_if_owned(state: dict[str, Any]) -> bool:
    with _operation_lock:
        return _restore_if_owned_locked(state)


def _restore_if_owned_locked(state: dict[str, Any]) -> bool:
    baseline = state.get("baseline_profile")
    if not _valid_profile(baseline):
        return False
    path, _ = _find_device()
    current = _get_profile(path)
    if _profile_name(current) != PROFILE_NAME:
        return False
    header, blocks = _mapping_blocks(current)
    _, base_blocks = _mapping_blocks(baseline)
    originals = {_source_for_block(block): block for block in base_blocks
                 if _source_for_block(block)}
    sources = {source: button for button, source in BUTTON_SOURCES.items()}
    output = []
    for block in blocks:
        source = _source_for_block(block)
        button = sources.get(source)
        if button and _events_for_block(block) == _action_events(button, state[f"{button}_action"]):
            # Restore only a mapping still equal to our last applied action.
            if source in originals:
                output.append(originals[source])
        else:
            output.append(block)
    original_name = re.search(r"(?m)^name:.*$", baseline)
    if original_name:
        header = re.sub(r"(?m)^name:.*$", lambda _: original_name.group(0), header, count=1)
    desired = header + "".join(output)
    if desired == current:
        return False
    restored = _load_profile(path, desired)
    if restored != desired:
        raise RemapError("InputPlumber did not restore the original profile.")
    return True


def _status_sync() -> dict[str, Any]:
    state = _load_state()
    response: dict[str, Any] = {
        "success": True,
        "supported": False,
        "enabled": state["enabled"],
        "active": False,
        "drift": False,
        "desktop_action": state["desktop_action"],
        "page_action": state["page_action"],
        "actions": [{"id": key, "label": value} for key, value in ACTION_LABELS.items()],
        "inputplumber_version": "",
        "profile_name": "",
        "reason": "",
        "error": _last_error,
    }
    try:
        version = subprocess.run(
            ["/usr/bin/inputplumber", "--version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3.0,
            check=False,
        )
        if version.returncode == 0:
            response["inputplumber_version"] = version.stdout.strip()
        path, _ = _find_device()
        current = _get_profile(path)
        response["supported"] = True
        response["profile_name"] = _profile_name(current)
        if not state["enabled"]:
            response["reason"] = "Enable remapping to choose new actions for both buttons."
        elif _profile_matches(current, state):
            response["active"] = True
            response["reason"] = "Both button mappings are active."
        else:
            response["drift"] = True
            response["reason"] = "InputPlumber changed profile; Companion will restore the mappings."
    except Exception as exc:
        response["reason"] = str(exc)
    return response


def _mutate_sync(changes: dict[str, Any]) -> dict[str, Any]:
    global _last_error
    with _operation_lock:
        before = _load_state()
        desired = copy.deepcopy(before)
        for key, value in changes.items():
            if key in ("desktop_action", "page_action") and isinstance(value, str) and value in ACTION_LABELS:
                desired[key] = value
            elif key == "enabled" and type(value) is bool:
                desired[key] = value
            else:
                raise ValueError("invalid remapper setting")

        if desired["enabled"]:
            path, _ = _find_device()
            current = _get_profile(path)
            if not before["enabled"] or _profile_name(current) != PROFILE_NAME:
                desired["baseline_profile"] = current
            _save_state(desired)
            try:
                desired, _ = _apply_state(desired, adopt_external=False)
                _last_error = ""
            except Exception as exc:
                # The durable desired state remains enabled so startup/watchdog
                # can finish an operation interrupted after the settings commit.
                _last_error = str(exc)
                raise
        else:
            try:
                if before["enabled"]:
                    _restore_if_owned(before)
                desired["baseline_profile"] = None
                _save_state(desired)
                _last_error = ""
            except Exception as exc:
                _last_error = str(exc)
                raise
    return _status_sync()


def _repair_sync() -> None:
    global _last_error
    with _operation_lock:
        state = _load_state()
        if not state["enabled"]:
            return
        try:
            state, _ = _apply_state(state, adopt_external=True)
            _last_error = ""
        except Exception as exc:
            _last_error = str(exc)
            decky.logger.warning(f"[legiongo2companion-remap] reapply failed: {exc}")


def _suspend_offset() -> float | None:
    clock = getattr(time, "CLOCK_BOOTTIME", None)
    if clock is None or not hasattr(time, "clock_gettime"):
        return None
    try:
        return time.clock_gettime(clock) - time.monotonic()
    except (OSError, ValueError):
        return None


def _resume_detected() -> bool:
    global _last_suspend_offset
    current = _suspend_offset()
    if current is None:
        return False
    previous = _last_suspend_offset
    _last_suspend_offset = current
    return previous is not None and current - previous >= 1.0


async def _watch_loop() -> None:
    last_check = time.monotonic()
    settle_until = last_check + 30.0
    while True:
        try:
            await asyncio.sleep(RESUME_CHECK_S)
            resumed = _resume_detected()
            now = time.monotonic()
            if resumed:
                settle_until = now + 30.0
                decky.logger.info("[legiongo2companion-remap] resume detected")
            if now < settle_until or now - last_check >= CHECK_INTERVAL_S:
                await asyncio.to_thread(_repair_sync)
                last_check = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            decky.logger.warning(f"[legiongo2companion-remap] watchdog failed: {exc}")


class Plugin:
    async def _main(self):
        global _watch_task, _last_suspend_offset
        _last_suspend_offset = _suspend_offset()
        await asyncio.to_thread(_repair_sync)
        if _watch_task is None or _watch_task.done():
            _watch_task = asyncio.create_task(_watch_loop())

    async def _unload(self):
        global _watch_task
        task, _watch_task = _watch_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        try:
            await asyncio.to_thread(_restore_if_owned, _load_state())
        except Exception as exc:
            decky.logger.warning(f"[legiongo2companion-remap] unload restore failed: {exc}")

    async def _uninstall(self):
        try:
            await asyncio.to_thread(_restore_if_owned, _load_state())
        except Exception as exc:
            decky.logger.warning(f"[legiongo2companion-remap] uninstall restore failed: {exc}")

    async def get_status(self):
        return await asyncio.to_thread(_status_sync)

    async def set_enabled(self, enabled):
        try:
            status = await asyncio.to_thread(_mutate_sync, {"enabled": enabled})
            return {"success": True, "status": status}
        except Exception as exc:
            return {"success": False, "error": str(exc), "status": await self.get_status()}

    async def set_action(self, button, action):
        if (not isinstance(button, str) or not isinstance(action, str)
                or button not in BUTTON_SOURCES or action not in ACTION_LABELS):
            return {"success": False, "error": "Unsupported button or action.", "status": await self.get_status()}
        key = f"{button}_action"
        try:
            status = await asyncio.to_thread(_mutate_sync, {key: action})
            return {"success": True, "status": status}
        except Exception as exc:
            return {"success": False, "error": str(exc), "status": await self.get_status()}

    async def restore_defaults(self):
        try:
            status = await asyncio.to_thread(_mutate_sync, {
                "desktop_action": "default",
                "page_action": "default",
            })
            return {"success": True, "status": status}
        except Exception as exc:
            return {"success": False, "error": str(exc), "status": await self.get_status()}

    async def reapply(self):
        await asyncio.to_thread(_repair_sync)
        return await self.get_status()
