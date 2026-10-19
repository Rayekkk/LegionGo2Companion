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
import re
import subprocess
import threading
import time
from typing import Any

import decky
from diagnostics import event, transition
from system_process import run
import module_runtime

from safe_settings import SettingsManager
from inputplumber_process import (
    DEVICE_PATH_RE, INTERFACE, SERVICE, InputPlumberError as RemapError, ProcessWatch,
    json_value as _json_value, run_busctl as _run_busctl,
)


INPUTPLUMBER = "/usr/bin/inputplumber"
VERSION_CACHE_TTL_S = 300.0
VERSION_RETRY_S = 10.0
EXPECTED_NAMES = {"Lenovo Legion Go 2"}
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
    "insert": "Insert",
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

DEFAULT_STATE: dict[str, Any] = {
    "enabled": False,
    "desktop_action": "default",
    "page_action": "default",
    "baseline_profile": None,
    "previous_actions": None,
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
_service_watch = ProcessWatch()
_version_lock = threading.Lock()
_version_cache: tuple[str, float] | None = None


def _valid_profile(value: Any) -> bool:
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    if len(value.encode("utf-8")) > MAX_PROFILE_BYTES:
        return False
    return bool(
        re.search(r"(?m)^version:\s*1\s*$", value)
        and re.search(r"(?m)^kind:\s*DeviceProfile\s*$", value)
        and re.search(r"(?m)^mapping:\s*(?:\[\s*\]\s*)?$", value)
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
    previous = source.get("previous_actions")
    if previous is not None:
        if (not isinstance(previous, dict) or set(previous) != set(BUTTON_SOURCES)
                or any(value is not None and (not isinstance(value, str) or value not in ACTION_LABELS)
                       for value in previous.values())):
            raise RemapError("The saved button transition is invalid; restoration data must be recovered first.")
        state["previous_actions"] = copy.deepcopy(previous)
    return state


def _load_state() -> dict[str, Any]:
    with _state_lock:
        if getattr(settings, "recovery_error", ""):
            settings.read()
        if getattr(settings, "recovery_error", ""):
            raise RemapError("Saved button ownership could not be recovered. " + settings.recovery_error)
        return _sanitize_state(settings.getSetting("state", DEFAULT_STATE))


def _save_state(state: dict[str, Any]) -> None:
    sanitized = _sanitize_state(state)
    with _state_lock:
        settings.setSetting("state", sanitized)
        settings.commit()


def _get_property(path: str, name: str) -> Any:
    return _json_value([
        "get-property", SERVICE, path, INTERFACE, name,
    ])


_REQUIRED_CAPABILITIES = {"Gamepad:Button:Keyboard", "Gamepad:Button:QuickAccess2"}
_device_path = ""  # last compatible CompositeDevice: re-verified each call, so the full tree runs only when it fails


def _compatible_name(path: str) -> str | None:
    name = _get_property(path, "Name")
    if name not in EXPECTED_NAMES:
        return None
    capabilities = _get_property(path, "Capabilities")
    if isinstance(capabilities, list) and _REQUIRED_CAPABILITIES.issubset(set(capabilities)):
        return str(name)
    return None


def _find_device() -> tuple[str, str]:
    global _device_path
    if not module_runtime.is_legion_go_2():
        raise RemapError("Button remapping is limited to the Lenovo Legion Go 2.")

    failure = None
    stalled = ""
    if _device_path:
        try:
            name = _compatible_name(_device_path)
            if name:
                return _device_path, name
        except RemapError as exc:
            if not _ABSENT.search(str(exc)):
                failure, stalled = exc, _device_path  # do not wait on the same stalled path twice
        _device_path = ""

    tree = _run_busctl(["tree", SERVICE, "--list", "--no-pager"])
    candidates = sorted({
        line.strip() for line in tree.splitlines()
        if DEVICE_PATH_RE.fullmatch(line.strip())
    })
    for path in candidates:
        if path == stalled:
            continue
        try:
            name = _compatible_name(path)
        except RemapError as exc:
            if not _ABSENT.search(str(exc)):
                failure = exc  # a stalled lookup does not prove the controller is gone
            continue
        if name:
            _device_path = path
            return path, name
    raise failure or RemapError("InputPlumber is not managing a compatible Go 2 controller.")


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
    mapping_indexes = [i for i, line in enumerate(lines)
                       if re.fullmatch(r"mapping:\s*(?:\[\s*\]\s*)?", line.rstrip("\r\n"))]
    if len(mapping_indexes) != 1:
        raise RemapError("Unexpected InputPlumber profile layout.")
    start = mapping_indexes[0]
    body = lines[start + 1 :]
    if lines[start].rstrip("\r\n") != "mapping:":
        # A valid empty list ("mapping: []"): nothing may follow it, and the header ends in a block list again.
        if any(line.strip() for line in body):
            raise RemapError("Unexpected InputPlumber profile layout.")
        lines[start], body = "mapping:\n", []
    header = "".join(lines[: start + 1])
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
        "insert": [("keyboard", "KeyInsert")],
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


def _expected_mapping(state: dict[str, Any], button: str, action: str | None = None) -> str | None:
    action = state[f"{button}_action"] if action is None else action
    if action != "default":
        return _render_mapping(button, action)
    _, blocks = _mapping_blocks(state.get("baseline_profile"))
    originals = [block for block in blocks if _source_for_block(block) == BUTTON_SOURCES[button]]
    if len(originals) > 1:
        raise RemapError("The original profile contains ambiguous dedicated-button mappings.")
    # An absent baseline mapping is also a system default: do not invent one.
    return originals[0] if originals else None


def _mapping_matches(actual: str | None, expected: str | None) -> bool:
    if actual is None or expected is None:
        return actual is expected
    events = _events_for_block(expected)
    if events is not None:
        return _events_for_block(actual) == events
    # Preserve unfamiliar baseline actions without interpreting two failed
    # parses as equivalent. Only the unchanged complete block is confirmed.
    return actual.replace("\r\n", "\n").strip() == expected.replace("\r\n", "\n").strip()


def _name_field(header: str) -> tuple[int, int]:
    lines = header.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if re.match(r"^name:", line)]
    if len(starts) != 1:
        raise RemapError("The profile must contain one top-level name.")
    start = starts[0]
    end = start + 1
    while end < len(lines) and (not lines[end].strip() or
                                lines[end][0].isspace() or lines[end].startswith("#")):
        end += 1
    return start, end


def _replace_name(header: str, name_header: str) -> str:
    lines = header.splitlines(keepends=True)
    start, end = _name_field(header)
    source = name_header.splitlines(keepends=True)
    first, last = _name_field(name_header)
    replacement = "".join(source[first:last])
    if not replacement.endswith("\n"):
        replacement += "\n"
    return "".join(lines[:start]) + replacement + "".join(lines[end:])


def _join_profile(header: str, blocks: list[str]) -> str:
    if not blocks:
        header = re.sub(r"(?m)^mapping:[ \t]*$", "mapping: []", header)  # A bare key would load as null.
    result = "".join(part if part.endswith("\n") else part + "\n"
                     for part in [header, *blocks])
    if len(result.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise RemapError("The resulting InputPlumber profile is too large.")
    return result


def _by_source(blocks: list[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for block in blocks:
        source = _source_for_block(block)
        if source:
            if source in found:
                raise RemapError("The active profile contains ambiguous dedicated-button mappings.")
            found[source] = block
    return found


def _build_profile(base_profile: str, state: dict[str, Any]) -> str:
    header, blocks = _mapping_blocks(base_profile)
    _by_source(blocks)
    replacements = {
        BUTTON_SOURCES[button]: _expected_mapping(state, button)
        for button in BUTTON_SOURCES
    }
    output_blocks = []
    replaced: set[str] = set()
    for block in blocks:
        source = _source_for_block(block)
        if source in replacements:
            if replacements[source] is not None:
                output_blocks.append(replacements[source])
            replaced.add(source)
        else:
            output_blocks.append(block)
    for button, source in BUTTON_SOURCES.items():
        if source not in replaced and replacements[source] is not None:
            output_blocks.append(replacements[source])

    header = _replace_name(header, f"name: {PROFILE_NAME}\n")
    return _join_profile(header, output_blocks)


def _profile_matches(profile: str, state: dict[str, Any]) -> bool:
    if _profile_name(profile) != PROFILE_NAME:
        return False
    try:
        found = _by_source(_mapping_blocks(profile)[1])
    except RemapError:
        return False
    for button, source in BUTTON_SOURCES.items():
        if not _mapping_matches(found.get(source), _expected_mapping(state, button)):
            return False
    return True


def _owned_actions(state: dict[str, Any], button: str) -> list[str]:
    actions = [state[f"{button}_action"]]
    previous = state.get("previous_actions") or {}
    if previous.get(button) is not None and previous[button] not in actions:
        actions.append(previous[button])
    return actions


def _previous_actions(current: str, state: dict[str, Any]) -> dict[str, str | None]:
    """Keep one currently observable owned action per button before a change.

    A failed D-Bus call can leave either endpoint of the previous transaction.
    Resolve that transition from the live profile before preparing another one,
    so repeated failures never accumulate an unbounded ownership history.
    """
    result: dict[str, str | None] = dict.fromkeys(BUTTON_SOURCES)
    if not state["enabled"] or _profile_name(current) != PROFILE_NAME:
        return result
    by_source = _by_source(_mapping_blocks(current)[1])
    for button, source in BUTTON_SOURCES.items():
        result[button] = next((action for action in _owned_actions(state, button)
                               if _mapping_matches(by_source.get(source),
                                                   _expected_mapping(state, button, action))), None)
    return result


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
    if adopt_external and _profile_name(current) != PROFILE_NAME:
        working["baseline_profile"] = current
        _save_state(working)
    if not _valid_profile(working.get("baseline_profile")):
        raise RemapError("The original InputPlumber profile is unavailable.")
    if _profile_matches(current, working):
        if working.get("previous_actions") is not None:
            working["previous_actions"] = None
            _save_state(working)
        return working, current
    # Merge our two mappings into the live profile. Other controls may have
    # changed since the baseline was captured.
    desired = _build_profile(current, working)
    applied = _load_profile(path, desired)
    if not _profile_matches(applied, working):
        raise RemapError("InputPlumber did not confirm both requested mappings.")
    if working.get("previous_actions") is not None:
        working["previous_actions"] = None
        _save_state(working)
    return working, applied


def _restore_current_if_owned() -> bool:
    # A cancelled monitor can still finish its worker. Read the final durable
    # ownership only after acquiring the same lock as that operation.
    with _operation_lock:
        return _restore_if_owned_locked(_load_state())


def _system_stopping() -> bool:
    try:
        result = run(["/usr/bin/systemctl", "is-system-running"], 2.0,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() == "stopping"


def _restore_at_unload() -> bool:
    """False when InputPlumber was lost because the system is shutting down.

    systemd stops InputPlumber before Decky, so that loss is expected; the next
    start confirms the profile again. Any other failure is still raised.
    """
    try:
        _restore_current_if_owned()
    except RemapError:
        if _system_stopping():
            return False
        raise
    return True


def _restore_if_owned_locked(state: dict[str, Any]) -> bool:
    baseline = state.get("baseline_profile")
    if not _valid_profile(baseline):
        return False
    path, _ = _find_device()
    current = _get_profile(path)
    if _profile_name(current) != PROFILE_NAME:
        return False
    header, blocks = _mapping_blocks(current)
    base_header, base_blocks = _mapping_blocks(baseline)
    originals = {_source_for_block(block): block for block in base_blocks
                 if _source_for_block(block)}
    sources = {source: button for button, source in BUTTON_SOURCES.items()}
    output = []
    for block in blocks:
        source = _source_for_block(block)
        button = sources.get(source)
        if button and any(_mapping_matches(block, _expected_mapping(state, button, action))
                          for action in _owned_actions(state, button)):
            # Restore only a mapping still equal to our last applied action.
            if source in originals:
                output.append(originals[source])
        else:
            output.append(block)
    header = _replace_name(header, base_header)
    desired = _join_profile(header, output)
    if desired == current:
        return False
    if not _restore_confirmed(_load_profile(path, desired), desired):
        raise RemapError("InputPlumber did not restore the original profile.")
    return True


def _restore_confirmed(restored: str, desired: str) -> bool:
    # Semantic like apply: a newer InputPlumber may re-serialize equivalent YAML.
    if restored == desired:
        return True
    if _profile_name(restored) != _profile_name(desired):
        return False
    try:
        got, want = _mapping_blocks(restored)[1], _mapping_blocks(desired)[1]
    except RemapError:
        return False
    return len(got) == len(want) and all(map(_mapping_matches, got, want))


# busctl/remap wording that proves none of our mappings can be live: the service,
# the bus or the controller is gone. A timeout is not proof: InputPlumber may be stalled.
_ABSENT = re.compile(r"not activatable|not provided by any \.service files|Unknown object|"
                     r"Failed to connect to bus|not managing a compatible Go 2 controller")


def _device_reachable() -> bool:
    try:
        _find_device()
    except RemapError as exc:
        return not _ABSENT.search(str(exc))
    return True


def _inputplumber_version() -> str:
    """Cache only the binary's label, never live profiles or hardware state."""
    global _version_cache
    with _version_lock:
        now = time.monotonic()
        if _version_cache is not None and now < _version_cache[1]:
            return _version_cache[0]
        value = ""
        try:
            result = run([INPUTPLUMBER, "--version"], 3.0, stdout=subprocess.PIPE,
                         stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace")
            if result.returncode == 0:
                value = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
        # A failed metadata probe must not hide a working D-Bus controller, nor
        # launch another process for every simultaneous status request.
        _version_cache = (value, now + (VERSION_CACHE_TTL_S if value else VERSION_RETRY_S))
        return value


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
        response["inputplumber_version"] = _inputplumber_version()
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
            desired["previous_actions"] = _previous_actions(current, before)
            _save_state(desired)
            try:
                desired, _ = _apply_state(desired, adopt_external=False)
                _service_watch.capture(_json_value)
                _last_error = ""
            except Exception as exc:
                # The durable desired state remains enabled so startup/watchdog
                # can finish an operation interrupted after the settings commit.
                _last_error = str(exc)
                raise
        else:
            try:
                if before["enabled"]:
                    try:
                        _restore_if_owned_locked(before)
                    except RemapError:
                        # InputPlumber is down: its restart loads its own profile, so
                        # nothing of ours stays live. Refusing would let the watchdog
                        # re-adopt that profile and re-apply mappings the user turned off.
                        if _device_reachable():
                            raise
                desired["baseline_profile"] = None
                desired["previous_actions"] = None
                _save_state(desired)
                _service_watch.clear()
                _last_error = ""
            except Exception as exc:
                _last_error = str(exc)
                raise
    return _status_sync()


def _repair_sync() -> bool:
    global _last_error
    with _operation_lock:
        state = _load_state()
        if not state["enabled"]:
            _service_watch.clear()
            transition("remap", "managed_profile", True, outcome="unmanaged", initial=False)
            return True
        try:
            state, _ = _apply_state(state, adopt_external=True)
            _service_watch.capture(_json_value)
            _last_error = ""
            transition("remap", "managed_profile", True, outcome="confirmed", initial=False)
            return True
        except Exception as exc:
            _last_error = str(exc)
            transition("remap", "managed_profile", False, level="warning", error=exc,
                       outcome="deferred", retryable=True)
            return False


_suspend_offset = module_runtime.suspend_offset


def _resume_detected() -> bool:
    global _last_suspend_offset
    _last_suspend_offset, resumed = module_runtime.resumed(_last_suspend_offset, _suspend_offset())
    return resumed


async def _watch_loop() -> None:
    last_check = time.monotonic()
    settle_until = last_check + 30.0
    retry_at = None
    retry_delay = RESUME_CHECK_S
    while True:
        try:
            await asyncio.sleep(RESUME_CHECK_S)
            if module_runtime.is_paused('remap'):
                continue
            resumed = _resume_detected()
            now = time.monotonic()
            if resumed:
                settle_until = now + 30.0
                retry_at = None
                retry_delay = RESUME_CHECK_S
                decky.logger.info("[legiongo2companion-remap] resume detected")
            if _service_watch.consume_exit():
                retry_at = now
                retry_delay = RESUME_CHECK_S
            due = now < settle_until or now - last_check >= CHECK_INTERVAL_S
            if retry_at is not None:
                due = now >= retry_at
            if due:
                repaired = await module_runtime.offload('remap', _repair_sync)
                last_check = now
                if repaired is False:
                    retry_at = now + retry_delay
                    retry_delay = min(CHECK_INTERVAL_S, retry_delay * 2)
                else:
                    retry_at = None
                    retry_delay = RESUME_CHECK_S
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            event("remap", "watchdog", level="warning", error=exc, outcome="failed", repeat=60)


class Plugin:
    async def _main(self):
        global _watch_task, _last_suspend_offset
        _last_suspend_offset = _suspend_offset()
        _service_watch.clear()
        await module_runtime.offload('remap', _repair_sync)
        if _watch_task is None or _watch_task.done():
            _watch_task = module_runtime.background_task(_watch_loop())

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
            if not await module_runtime.offload('remap', _restore_at_unload):
                event('remap', 'restore', outcome='deferred', reason='system_stopping', stage='unload', pending=True)
        except Exception as exc:
            event('remap','restore_failed',level='warning',error=exc,stage='unload',pending=True,restored=False)
        finally:
            _service_watch.clear()

    async def _uninstall(self):
        try:
            await module_runtime.offload('remap', _restore_current_if_owned)
        except Exception as exc:
            event('remap','restore_failed',level='warning',error=exc,stage='uninstall',pending=True,restored=False)
        finally:
            _service_watch.clear()

    async def get_status(self):
        return await module_runtime.offload('remap', _status_sync)

    async def _mutate(self, changes):
        try:
            status = await module_runtime.offload('remap', _mutate_sync, changes)
            return {"success": True, "status": status}
        except Exception as exc:
            return {"success": False, "error": str(exc), "status": await self.get_status()}

    async def set_enabled(self, enabled):
        return await self._mutate({"enabled": enabled})

    async def set_action(self, button, action):
        if (not isinstance(button, str) or not isinstance(action, str)
                or button not in BUTTON_SOURCES or action not in ACTION_LABELS):
            return {"success": False, "error": "Unsupported button or action.", "status": await self.get_status()}
        return await self._mutate({f"{button}_action": action})

    async def restore_defaults(self):
        return await self._mutate({"desktop_action": "default", "page_action": "default"})
