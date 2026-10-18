"""WiFi band-policy module for Legion Go 2 Companion.

Runs as root inside the plugin_loader process. All public async methods on
the Plugin class are callable from the React frontend via Decky's IPC. State
is persisted to settings.json under DECKY_PLUGIN_SETTINGS_DIR. Band policy is
persisted by NetworkManager/iwd and guarded by an independent rollback journal.
"""

import os
import json
import time
import asyncio
from diagnostics import configure_worker, event as diagnostic_event, transition as diagnostic_transition
from system_process import system_env
import subprocess
import re
import sys
import math
import copy
import stat
import threading

from safe_settings import (
    AtomicSettingsManager,
    CorruptSettings,
    UnsafeSettingsPath,
    _atomic_write_bytes,
    atomic_write_json,
    load_json_object,
)

try:
    import fcntl
    if not all(hasattr(fcntl, name) for name in ("flock", "LOCK_EX", "LOCK_UN")):
        fcntl = None
except ImportError:  # pragma: no cover - available on the Linux target
    fcntl = None

try:
    import decky
except ImportError:
    # Local fallback when decky isn't importable (e.g., running outside
    # plugin_loader for static analysis or ad-hoc testing). All runtime
    # paths on a Deck have the real module.
    class decky:  # type: ignore
        DECKY_PLUGIN_SETTINGS_DIR = "/tmp/wifi-optimizer"
        DECKY_PLUGIN_DIR = "/tmp/wifi-optimizer"
        DECKY_PLUGIN_VERSION = "0.0.0"
        class logger:
            @staticmethod
            def info(msg): print(f"[INFO] {msg}")
            @staticmethod
            def error(msg): print(f"[ERROR] {msg}")

WIFI_BACKEND_CONF = "/etc/NetworkManager/conf.d/99-valve-wifi-backend.conf"
NM_DEFAULT_CONF = "/usr/lib/NetworkManager/conf.d/10-steamos-defaults.conf"
BAZZITE_IWD_CONF = "/etc/NetworkManager/conf.d/iwd.conf"
IWD_MAIN_CONF = "/etc/iwd/main.conf"
# Snapshot key prefix and iwd [Rank] key; only the 2.4 GHz key is ever written.
IWD_RANK_KEYS = (("", "BandModifier2_4GHz"), ("five_", "BandModifier5GHz"),
                 ("six_", "BandModifier6GHz"))

BAND_POLICY_OFF = "off"
BAND_POLICY_SIX_ONLY = "six_ghz_only"
# Keep this persisted legacy value for the existing fallback preference.
BAND_POLICY_HIGH_ONLY = "five_six_no_24"
BAND_POLICY_STRICT_HIGH = "five_six_only"
IWD_BAND_POLICIES = (BAND_POLICY_HIGH_ONLY, BAND_POLICY_STRICT_HIGH)
# Non-zero keeps 2.4 GHz available as a fallback, while making any usable
# 5/6 GHz BSS overwhelmingly preferable in iwd's ranking calculation.
BAND_PREFERENCE_2_4_MODIFIER = "0.01"
BAND_STRICT_2_4_MODIFIER = "0.0"
BAND_POLICIES = {
    BAND_POLICY_OFF,
    BAND_POLICY_SIX_ONLY,
    BAND_POLICY_HIGH_ONLY,
    BAND_POLICY_STRICT_HIGH,
}
# Long enough for iwd restart (15s), NetworkManager activation (25s), and
# verification (24s), with margin for slower handheld boots.
BAND_POLICY_ROLLBACK_SECONDS = 180
MANUAL_RECONNECT_ROLLBACK_SECONDS = 60
# Historical unit/lock names: the plugin, startup recovery and the systemd
# rollback timer share them, so renaming would let an old timer from before an
# update race the new lock. Keep them.
BAND_POLICY_ROLLBACK_UNIT_PREFIX = "wifi-optimizer-go2-band-rollback"
HIGH_BAND_SCAN_SECONDS = 12
HIGH_BAND_MONITOR_SECONDS = 5
HIGH_BAND_RETRY_SECONDS = 30
HIGH_BAND_IDLE_SECONDS = 45  # Poll cadence once there is nothing left to try.

LEGION_GO_2_PRODUCTS = ("83N0", "83N1")

try:
    SETTINGS_FILE = os.path.join(
        decky.DECKY_PLUGIN_SETTINGS_DIR, "wifi_settings.json"
    )
except Exception:
    SETTINGS_FILE = "/tmp/legiongo2companion/wifi_settings.json"

BAND_POLICY_RUNTIME_DIR = "/run/wifi-optimizer-go2"
BAND_POLICY_STATE_DIR = "/var/lib/legiongo2companion-wifi"
BAND_POLICY_JOURNAL_FILE = os.path.join(
    BAND_POLICY_STATE_DIR, "band-policy-transaction.json"
)
BAND_POLICY_LOCK_FILE = os.path.join(BAND_POLICY_RUNTIME_DIR, "band-policy.lock")
JOURNAL_PHASES = frozenset(("pending", "applying", "verified", "committed",
                            "rolling_back", "rolled_back", "rollback_failed"))
DECKY_PLUGIN_DIR = getattr(decky, "DECKY_PLUGIN_DIR", os.path.dirname(__file__))

DEFAULT_SETTINGS = {
    "driver": "unknown",
    "device_family": "unknown",
    "device_label": "Unknown Device",
    "band_policy": BAND_POLICY_OFF,
    "band_policy_state": {},
    "band_preference_enabled": False,
    "last_applied": 0,
}


_SETTINGS_MANAGER = None
_SETTINGS_MANAGER_PATH = ""
_SETTINGS_MANAGER_LOCK = threading.Lock()


def _get_settings_manager() -> AtomicSettingsManager:
    """Return a store bound to the current settings path.

    Keeping this path-aware is useful for both Decky's runtime directory and
    the isolated paths used by the recovery tests.
    """
    global _SETTINGS_MANAGER, _SETTINGS_MANAGER_PATH
    path = os.path.abspath(SETTINGS_FILE)
    with _SETTINGS_MANAGER_LOCK:
        if _SETTINGS_MANAGER is None or _SETTINGS_MANAGER_PATH != path:
            filename = os.path.basename(path)
            name, extension = os.path.splitext(filename)
            if extension.lower() != ".json" or not name:
                raise UnsafeSettingsPath(
                    "settings file must have a simple .json filename"
                )
            _SETTINGS_MANAGER = AtomicSettingsManager(
                name, os.path.dirname(path)
            )
            _SETTINGS_MANAGER_PATH = path
        return _SETTINGS_MANAGER


def _load_settings() -> dict:
    manager = _get_settings_manager()
    manager.read()
    if manager.recovery_error:
        raise CorruptSettings(
            "WiFi settings recovery failed; the original network ownership "
            "cannot be determined. Existing network settings were left untouched. "
            + manager.recovery_error
        )
    original = copy.deepcopy(manager.settings)
    data = copy.deepcopy(original)

    # Retain the old boolean as a compatibility view of the preference mode.
    # The exact policy field distinguishes preference from strict 5/6 GHz.
    data["band_preference_enabled"] = data.get("band_policy") == BAND_POLICY_HIGH_ONLY

    merged = {**DEFAULT_SETTINGS, **data}
    normalized = {k: v for k, v in merged.items() if k in DEFAULT_SETTINGS}
    # Persist migrations and remove stale fields once, rather than rewriting
    # settings on every status poll.
    if original and normalized != original:
        manager.replace(normalized)
    return normalized


def _save_settings(data: dict):
    _get_settings_manager().replace(data)


class Plugin:
    """Root plugin instance. Decky exposes every async method here as a
    callable from the frontend. Synchronous helpers prefixed with `_` are
    for internal use only."""

    # ---- Helpers ----

    def _run_cmd(self, cmd: list[str], timeout: int = 5, preserve_stdout: bool = False) -> dict:
        """Run a subprocess and return a result dict.

        Commands always use system libraries: Decky's bundled ones can break
        busctl, systemctl and curl.
        """
        try:
            env = system_env(LC_ALL="C")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, env=env
            )
            return {
                "success": result.returncode == 0,
                "stdout": result.stdout if preserve_stdout else result.stdout.strip(),
                "stderr": result.stderr.strip(),
                "returncode": result.returncode,
            }
        except subprocess.TimeoutExpired:
            stderr = "Command timed out"
        except FileNotFoundError:
            stderr = f"Command not found: {cmd[0]}"
        except Exception as e:
            stderr = str(e)
        return {"success": False, "stdout": "", "stderr": stderr, "returncode": -1}

    # ---- Band policy helpers -------------------------------------------------

    def _get_network_mutation_lock(self) -> asyncio.Lock:
        """Serialize profile, iwd, and their shared settings ownership state."""
        lock = getattr(self, "_network_mutation_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._network_mutation_lock = lock
        return lock

    def _get_band_policy_file_gate(self) -> asyncio.Lock:
        """Allow only one local caller to wait for the cross-process flock."""
        lock = getattr(self, "_band_policy_file_gate", None)
        if lock is None:
            lock = asyncio.Lock()
            self._band_policy_file_gate = lock
        return lock

    async def _worker_complete(self, fn, *args, cancelled_cleanup=None):
        worker = asyncio.create_task(asyncio.to_thread(fn, *args))
        cancelled = False
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                cancelled = True
        try:
            result = worker.result()
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError
            raise
        if cancelled:
            if cancelled_cleanup is not None:
                cancelled_cleanup(result)
            raise asyncio.CancelledError
        return result

    @staticmethod
    def _high_band_discovery_enabled(settings: dict) -> bool:
        state = settings.get("band_policy_state") or {}
        return bool(settings.get("device_family") == "legion_go_2"
                    and settings.get("driver") == "mt7921e"
                    and settings.get("band_policy") == BAND_POLICY_HIGH_ONLY
                    and state.get("mode") == BAND_POLICY_HIGH_ONLY
                    and state.get("owns_iwd") is True)

    def _get_high_band_link(self, stop: threading.Event | None = None) -> dict | None:
        """Bind observations to one activated NM connection and kernel device."""
        devices = self._run_cmd([
            "/usr/bin/nmcli", "-t", "-f", "DEVICE,TYPE", "dev", "status",
        ], timeout=1)
        if not devices.get("success") or (stop is not None and stop.is_set()):
            return None
        iface = next((line.rsplit(":", 1)[0] for line in devices.get("stdout", "").splitlines()
                      if line.endswith(":wifi")), "")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", iface):
            return None
        self._high_band_iface = iface
        try:
            driver = os.path.basename(os.path.realpath(f"/sys/class/net/{iface}/device/driver"))
            with open(f"/sys/class/net/{iface}/ifindex", encoding="ascii") as handle:
                ifindex = int(handle.read(32))
        except (OSError, ValueError):
            return None
        if driver != "mt7921e":
            return None
        connection = self._run_cmd([
            "/usr/bin/nmcli", "-g", "GENERAL.STATE,GENERAL.CON-UUID,GENERAL.CON-PATH",
            "device", "show", iface,
        ], timeout=1)
        fields = connection.get("stdout", "").splitlines()
        if (not connection.get("success") or len(fields) != 3
                or not re.fullmatch(r"100(?:\s+\(connected\))?", fields[0].strip())
                or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", fields[1])
                or not re.fullmatch(r"/org/freedesktop/NetworkManager/ActiveConnection/\d+", fields[2])):
            return None
        if stop is not None and stop.is_set():
            return None
        radio = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"], timeout=1)
        frequency = re.search(r"(?m)^\s*freq:\s*(\d+)", radio.get("stdout", ""))
        if not radio.get("success") or not frequency:
            return None
        return {"iface": iface, "uuid": fields[1], "frequency": int(frequency.group(1)),
                "episode": (iface, ifindex, fields[1], fields[2])}

    def _high_band_carrier_down(self) -> bool:
        """True only when the last seen WiFi interface surely has no link (NM state 100 needs IF_OPER_UP)."""
        iface = getattr(self, "_high_band_iface", "")
        if not iface:
            return False
        try:
            with open(f"/sys/class/net/{iface}/operstate", encoding="ascii") as handle:
                return handle.read(32).strip() in ("down", "dormant", "lowerlayerdown", "notpresent")
        except OSError:
            return False

    def _discover_high_bands(self, link: dict, stop: threading.Event) -> dict:
        """Refresh iwd's stale quick-autoconnect cache without forcing a BSSID."""
        started = time.monotonic()
        settings = _load_settings()
        if (stop.is_set() or not self._high_band_discovery_enabled(settings)
                or self._get_current_backend() != "iwd"
                or self._band_policy_ownership_error(settings)
                or self._get_network_recovery_issues()):
            return {"success": False, "error": "discovery_unavailable"}
        current = self._get_high_band_link(stop)
        if not current or current["episode"] != link["episode"]:
            return {"success": False, "error": "connection_changed"}
        if current["frequency"] >= 5925:
            return {"success": True, "scanned": False, "frequency": current["frequency"]}
        if stop.is_set():
            return {"success": False, "error": "discovery_cancelled"}
        tree = self._run_cmd([
            "/usr/bin/busctl", "--json=short", "call", "net.connman.iwd", "/",
            "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
        ], timeout=1)
        try:
            objects = json.loads(tree.get("stdout", ""))["data"][0]
            stations = [path for path, interfaces in objects.items()
                        if interfaces.get("net.connman.iwd.Device", {}).get("Name", {}).get("data") == link["iface"]
                        and "net.connman.iwd.Station" in interfaces
                        and re.fullmatch(r"/net/connman/iwd/[a-zA-Z0-9_/]+", path)]
            if not tree.get("success") or len(stations) != 1:
                raise ValueError("No unique station")
            station = stations[0]
            network_path = objects[station]["net.connman.iwd.Station"].get("ConnectedNetwork", {}).get("data")
            ssid = objects.get(network_path, {}).get("net.connman.iwd.Network", {}).get("Name", {}).get("data")
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return {"success": False, "error": "station_unavailable"}
        if stop.is_set():
            return {"success": False, "error": "discovery_cancelled"}
        current = self._get_high_band_link(stop)
        if not current or current["episode"] != link["episode"]:
            return {"success": False, "error": "connection_changed"}
        if stop.is_set():
            return {"success": False, "error": "discovery_cancelled"}
        scan = self._run_cmd([
            "/usr/bin/busctl", "call", "net.connman.iwd", station,
            "net.connman.iwd.Station", "Scan",
        ], timeout=2)
        scanning_seen = False
        if not scan.get("success"):
            # busctl's human-readable error need not include the DBus name.
            # Join an existing scan only when the public property proves it.
            state = self._run_cmd([
                "/usr/bin/busctl", "get-property", "net.connman.iwd", station,
                "net.connman.iwd.Station", "Scanning",
            ], timeout=1)
            scanning_seen = bool(state.get("success") and state.get("stdout") == "b true")
            if not scanning_seen:
                return {"success": False, "error": "scan_failed"}
        deadline = started + HIGH_BAND_SCAN_SECONDS
        while not stop.is_set() and time.monotonic() < deadline:
            state = self._run_cmd([
                "/usr/bin/busctl", "get-property", "net.connman.iwd", station,
                "net.connman.iwd.Station", "Scanning",
            ], timeout=1)
            value = state.get("stdout", "")
            if not state.get("success") or value not in ("b true", "b false"):
                return {"success": False, "error": "scan_state_unavailable"}
            scanning_seen = scanning_seen or value == "b true"
            if value == "b false" and (scanning_seen or (scan.get("success") and time.monotonic() - started >= 1)):
                after = self._get_high_band_link(stop)
                if not after or after["episode"] != link["episode"]:
                    return {"success": False, "error": "connection_changed"}
                if isinstance(ssid, str):
                    after["ssid"] = ssid
                result = self._reselect_discovered_high_band(after, stop)
                return {**result, "scanned": True,
                        "elapsed_seconds": round(time.monotonic() - started, 3)}
            stop.wait(0.25)
        return {"success": False, "error": "discovery_cancelled" if stop.is_set() else "scan_timeout"}

    def _run_discovery_scan(self, iface: str, ssid: str, stop: threading.Event) -> dict:
        """A directed scan finds 6 GHz BSSs omitted by iwd's wildcard scan."""
        if stop.is_set():
            return {"success": False}
        try:
            process = subprocess.Popen(
                ["/usr/bin/iw", "dev", iface, "scan", "ssid", ssid],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=system_env(LC_ALL="C"),
            )
        except OSError as exc:
            diagnostic_event('wifi', 'discovery_scan_failed', level='warning', repeat=60,
                             stage='spawn', code=type(exc).__name__, errno=exc.errno)
            return {"success": False}
        deadline = time.monotonic() + 8
        try:
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    stdout, _stderr = process.communicate(timeout=0.2)
                    diagnostic_transition('wifi', 'discovery_scan', 'ready' if process.returncode == 0 else 'failed',
                                          level='info' if process.returncode == 0 else 'warning',
                                          stage='completed', returncode=process.returncode)
                    return {"success": process.returncode == 0, "stdout": stdout}
                except subprocess.TimeoutExpired:
                    pass
            if not stop.is_set():
                diagnostic_event('wifi', 'discovery_scan_failed', level='warning', repeat=60, stage='timeout')
            return {"success": False}
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=1)

    def _reselect_discovered_high_band(self, link: dict, stop: threading.Event) -> dict:
        fallback = {"success": True, "frequency": link["frequency"], "reselected": False}
        if link["frequency"] >= 5925 or stop.is_set():
            return fallback
        # DBus Name preserves significant leading/trailing SSID spaces.
        ssid = link.get("ssid")
        if not isinstance(ssid, str) or not 1 <= len(ssid.encode("utf-8")) <= 32 or stop.is_set():
            return fallback
        scan = self._run_discovery_scan(link["iface"], ssid, stop)
        if not scan.get("success") or stop.is_set():
            return {**fallback, "directed_scan_success": False}
        frequencies = [item["frequency"] for item in self._iw_scan_bss(scan.get("stdout", ""), ssid)
                       if item["frequency"] is not None]
        fallback.update(directed_scan_success=True, candidate_frequencies=sorted(set(frequencies)))
        better = any(5925 <= freq < 7125 or (link["frequency"] < 4900 and 4900 <= freq < 5925)
                     for freq in frequencies)
        if not better:
            return fallback
        settings = _load_settings()
        current = self._get_high_band_link(stop)
        if (not current or current["episode"] != link["episode"] or stop.is_set()
                or not self._high_band_discovery_enabled(settings)
                or self._get_current_backend() != "iwd"
                or self._band_policy_ownership_error(settings) or self._get_network_recovery_issues()
                or self._get_iwd_profile_conflict(link["uuid"])):
            return fallback
        # iwd can roam during the directed scan without changing NM's active
        # connection path. Do not cycle a link that already reached the best
        # freshly observed band.
        if (current["frequency"] >= 5925
                or (current["frequency"] >= 4900
                    and not any(5925 <= freq < 7125 for freq in frequencies))):
            return {**fallback, "frequency": current["frequency"]}
        # Never override a user's explicit profile band/BSSID selection.
        for field in ("802-11-wireless.band", "802-11-wireless.bssid"):
            value, result = self._nmcli_get(link["uuid"], field, timeout=1)
            if not result.get("success") or value or stop.is_set():
                return fallback
        if stop.is_set():
            return fallback
        saved = self._band_settings_fields(settings)
        journal = {
            "schema": 1, "operation_id": f"discovery-{os.getpid()}-{time.time_ns()}",
            "operation": "automatic_high_band_reconnect", "phase": "pending",
            "created_at": int(time.time()), "rollback_delay_seconds": MANUAL_RECONNECT_ROLLBACK_SECONDS,
            "settings_file": SETTINGS_FILE, "settings_before": saved, "settings_after": saved,
            "mode_before": BAND_POLICY_HIGH_ONLY, "mode_target": BAND_POLICY_HIGH_ONLY,
            "profile_before": {"uuid": link["uuid"], "band": "", "bssid": "", "was_active": True},
            "active_uuid_before": link["uuid"], "iwd_before": self._get_iwd_rank_modifier_snapshot(),
            "automatic_iface": link["iface"], "automatic_ifindex": link["episode"][1],
            "automatic_boot_id": self._boot_id(),
            "rollback_reconnect_required": True, "rollback_reconnect_uuid": link["uuid"], "mutations": {},
        }
        self._save_band_policy_journal(journal)
        watchdog = self._schedule_band_policy_rollback(journal)
        if not watchdog.get("success"):
            self._remove_band_policy_journal()
            return {**fallback, "success": False, "error": "rollback_watchdog_failed"}
        outcome = {**fallback, "_reselection_from_episode": link["episode"]}
        try:
            journal["phase"] = "applying"
            self._save_band_policy_journal(journal)
            if self._get_current_backend() != "iwd":
                raise RuntimeError("The native WiFi backend changed before automatic reconnection")
            # Finish restoring connectivity even if unloading starts after down.
            # The independent journal also survives Decky's process grace limit.
            result = self._reconnect_profile(link["uuid"], cycle=True, activation_timeout=5,
                                             bound_iface=link["iface"])
            after = self._get_high_band_link()
            if (not result.get("success") or not after
                    or after["episode"][:3] != link["episode"][:3]):
                raise RuntimeError("The saved WiFi connection did not obtain a verified link")
            journal["phase"] = "committed"
            self._save_band_policy_journal(journal)
            self._cancel_band_policy_rollback(journal)
            self._remove_band_policy_journal()
            return {**outcome, "frequency": after["frequency"], "reselected": True}
        except Exception as exc:
            rollback = self._roll_back_failed_transaction(journal, str(exc))
            return {**outcome, "success": False, "error": "automatic_reselection_failed",
                    "rolled_back": rollback.get("success", False)}

    @staticmethod
    def _boot_id() -> str:
        try:
            with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
                return handle.read(128).strip()
        except OSError:
            return ""

    @staticmethod
    def _suspend_offset() -> float:
        # CLOCK_BOOTTIME includes suspend; MONOTONIC does not. Windows tests
        # and systems without this clock retain connection-episode detection.
        if hasattr(time, "CLOCK_BOOTTIME"):
            return time.clock_gettime(time.CLOCK_BOOTTIME) - time.monotonic()
        return 0.0

    async def _high_band_monitor(self, stop: threading.Event):
        episode = None
        attempts = 0
        probe_limit = 2
        reconnects = 0
        next_scan = 0.0
        suspend_offset = self._suspend_offset()
        was_six = False
        own_reselection = None
        while not stop.is_set():
            settings = await self._worker_complete(_load_settings)
            if not self._high_band_discovery_enabled(settings):
                return
            if await self._worker_complete(self._get_current_backend) != "iwd":
                # A retained preference is inactive while SteamOS selects WPA.
                # Keep only the cheap config poll so native iwd return resumes it.
                episode, attempts, next_scan = None, 0, 0.0
                own_reselection, was_six = None, False
                await asyncio.sleep(HIGH_BAND_MONITOR_SECONDS)
                continue
            offset = self._suspend_offset()
            resumed = offset - suspend_offset > 1
            suspend_offset = offset
            link = await self._worker_complete(self._get_high_band_link, stop)
            if not link:
                if own_reselection is None:
                    episode = None
                was_six = False
                if self._high_band_carrier_down():
                    # Radio off or out of range: watch only sysfs (no nmcli
                    # spawns) and re-check everything at least every idle period.
                    deadline = time.monotonic() + HIGH_BAND_IDLE_SECONDS
                    while not stop.is_set() and time.monotonic() < deadline:
                        await asyncio.sleep(HIGH_BAND_MONITOR_SECONDS)
                        if not self._high_band_carrier_down():
                            break
                    continue
            else:
                if own_reselection is not None:
                    if link["episode"][:3] == own_reselection[:3]:
                        # Our activation/recovery path is one attempt, not a
                        # fresh user connection with a fresh budget.
                        if link["episode"] != own_reselection:
                            episode = link["episode"]
                            own_reselection = None
                    else:
                        own_reselection = None
                if link["episode"] != episode or resumed or (was_six and link["frequency"] < 5925):
                    episode, attempts, next_scan = link["episode"], 0, 0.0
                    probe_limit, reconnects = 2, 0
                was_six = link["frequency"] >= 5925
                if not was_six and attempts < probe_limit and reconnects < 2 and time.monotonic() >= next_scan:
                    async with self._get_band_policy_file_gate():
                        try:
                            handle = await self._worker_complete(
                                lambda: self._acquire_band_policy_file_lock(nonblocking=True),
                                cancelled_cleanup=self._release_band_policy_file_lock)
                        except BlockingIOError:
                            await asyncio.sleep(HIGH_BAND_MONITOR_SECONDS)
                            continue
                        try:
                            async with self._get_network_mutation_lock():
                                self._high_band_discovery_pending = True
                                try:
                                    result = await self._worker_complete(self._discover_high_bands, link, stop)
                                    own_reselection = result.pop("_reselection_from_episode", None)
                                    if own_reselection is not None:
                                        reconnects += 1
                                    if (result.get("reselected") and result.get("success")
                                            and link["frequency"] < 4900
                                            and 4900 <= result.get("frequency", 0) < 5925):
                                        # Early boot scans can miss high bands. After
                                        # reaching 5 GHz, allow one more discovery
                                        # for 6 GHz without a third reconnect.
                                        probe_limit = 3
                                    diagnostic_event('wifi', 'automatic_discovery',
                                                     level='info' if result.get('success') else 'warning',
                                                     outcome='ready' if result.get('success') else 'failed',
                                                     restored=result.get('rolled_back'),
                                                     reason=result.get('error', ''), stage='completed')
                                finally:
                                    self._high_band_discovery_pending = False
                        finally:
                            self._release_band_policy_file_lock(handle)
                    attempts += 1
                    next_scan = time.monotonic() + HIGH_BAND_RETRY_SECONDS
                # Already on 6 GHz or out of probes: only a new connection or
                # a drop below 6 GHz matters, so poll slowly until it happens.
                if was_six or attempts >= probe_limit or reconnects >= 2:
                    await asyncio.sleep(HIGH_BAND_IDLE_SECONDS)
                    continue
            await asyncio.sleep(HIGH_BAND_MONITOR_SECONDS)

    def _start_high_band_monitor(self, settings: dict):
        if not getattr(self, "_high_band_monitor_ready", False) or not self._high_band_discovery_enabled(settings):
            return
        task = getattr(self, "_high_band_monitor_task", None)
        if task and not task.done():
            return
        stop = threading.Event()
        self._high_band_monitor_stop = stop
        task = asyncio.create_task(self._high_band_monitor(stop))
        self._high_band_monitor_task = task
        def completed(done):
            if not done.cancelled() and done.exception() is not None:
                diagnostic_event('wifi', 'automatic_discovery_stopped', level='error', code=type(done.exception()).__name__)
        task.add_done_callback(completed)

    async def _stop_high_band_monitor(self):
        stop = getattr(self, "_high_band_monitor_stop", None)
        if stop is not None:
            stop.set()
        task = getattr(self, "_high_band_monitor_task", None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._high_band_monitor_task = None
        self._high_band_discovery_pending = False

    async def _restart_high_band_monitor(self):
        if getattr(self, "_high_band_monitor_ready", False):
            try:
                self._start_high_band_monitor(await self._worker_complete(_load_settings))
            except Exception as exc:
                # Background discovery must not replace a transaction result
                # or start scanning with unreadable ownership settings.
                diagnostic_event('wifi', 'automatic_discovery_restart_failed', level='warning', repeat=60, code=type(exc).__name__)

    def _acquire_band_policy_file_lock(self, nonblocking: bool = False):
        """Fence the worker, startup recovery, and systemd watchdog."""
        os.makedirs(BAND_POLICY_RUNTIME_DIR, mode=0o700, exist_ok=True)
        os.chmod(BAND_POLICY_RUNTIME_DIR, 0o700)
        handle = open(BAND_POLICY_LOCK_FILE, "a+", encoding="utf-8")
        os.chmod(BAND_POLICY_LOCK_FILE, 0o600)
        try:
            if fcntl is not None:
                flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
                fcntl.flock(handle.fileno(), flags)
        except BaseException:
            handle.close()
            raise
        return handle

    @staticmethod
    def _release_band_policy_file_lock(handle):
        try:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _atomic_write_json(self, path: str, data: dict):
        self._atomic_write_text(path, json.dumps(data, indent=2) + "\n", 0o600)

    @staticmethod
    def _atomic_write_text(path: str, text: str, mode: int = 0o644):
        """Replace a root-owned file without following links; a file that
        already exists keeps its mode and owner."""
        encoded = text.encode("utf-8")
        if len(encoded) > 1024 * 1024:
            raise ValueError("refusing to write a file larger than 1 MiB")
        _atomic_write_bytes(path, encoded, mode, keep_existing=True, strict_sync=True)

    @staticmethod
    def _read_bounded_regular_text(path: str, limit: int = 1024 * 1024) -> str:
        """Read a small regular file without following its final symlink."""
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise UnsafeSettingsPath(f"path is not a regular file: {path}")
        if info.st_size > limit:
            raise ValueError(f"file exceeds the {limit}-byte safety limit")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or opened.st_size > limit:
                raise UnsafeSettingsPath(f"opened path is unsafe: {path}")
            with os.fdopen(fd, "r", encoding="utf-8", newline="") as handle:
                fd = -1
                return handle.read(limit + 1)
        finally:
            if fd >= 0:
                os.close(fd)

    def _nmcli_get(self, uuid: str, key: str, timeout: int = 4) -> tuple[str, dict]:
        result = self._run_cmd(["/usr/bin/nmcli", "-g", key, "con", "show", "uuid", uuid],
                               timeout=timeout)
        value = result.get("stdout", "").strip()
        if value == "--":
            value = ""
        return value, result

    def _restore_nm_field(self, uuid: str, key: str, label: str, expected: str, before: str,
                          conflicts: list, read=None, before_restore=None):
        """Put a profile field back to `before` only while it still holds the
        value `expected` that we wrote, then verify; anything else is a conflict.
        `read` normalizes the nmcli value (None means unreadable)."""
        read = read or (lambda raw: raw)
        raw, result = self._nmcli_get(uuid, key)
        current = read(raw) if result.get("success") else None
        if current is None:
            conflicts.append(f"could not read the saved WiFi profile {label}")
        elif current == expected:
            if before_restore:
                before_restore()
            restored = self._nmcli_modify(uuid, key, before, timeout=8)
            if not restored.get("success"):
                conflicts.append(f"NetworkManager {label} restore failed: {restored.get('stderr', '')}")
            else:
                raw, result = self._nmcli_get(uuid, key)
                if not result.get("success") or read(raw) != before:
                    conflicts.append(f"NetworkManager {label} restore could not be verified")
        elif current != before:
            conflicts.append(f"profile {label} changed outside Legion Go 2 Companion")

    @staticmethod
    def _parse_version(value: str) -> tuple[int, ...]:
        match = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", value or "")
        if not match:
            return ()
        return tuple(int(part or 0) for part in match.groups())

    @staticmethod
    def _nmcli_unescape(value: str) -> str:
        return re.sub(r"\\(.)", r"\1", value)

    @staticmethod
    def _iw_ssid(value: str) -> str | None:
        """Decode iw print_ssid_escaped once, as UTF-8 bytes rather than text escapes."""
        try:
            raw = re.sub(rb"\\x([0-9a-fA-F]{2})",
                         lambda match: bytes([int(match.group(1), 16)]), value.encode("utf-8"))
            return raw.decode("utf-8") if 1 <= len(raw) <= 32 else None
        except UnicodeError:
            return None

    @staticmethod
    def _nmcli_fields(value: str) -> list[str]:
        """Split terse nmcli rows while decoding escaped colons/backslashes."""
        fields = [""]
        escaped = False
        for character in value:
            if escaped:
                fields[-1] += character
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == ":":
                fields.append("")
            else:
                fields[-1] += character
        return [] if escaped else fields

    def _get_profile_ssid(self, uuid: str) -> str | None:
        # Nonempty surrounding fields protect significant SSID whitespace
        # from the command runner's outer stdout trimming.
        metadata = self._run_cmd([
            "/usr/bin/nmcli", "-g", "connection.uuid,802-11-wireless.ssid,802-11-wireless.hidden",
            "con", "show", "uuid", uuid,
        ], timeout=3)
        # nmcli records use ASCII LF. Valid UTF-8 SSIDs may themselves contain
        # Unicode line separators, which Python's splitlines() also splits.
        fields = metadata.get("stdout", "").split("\n")
        if (not metadata.get("success") or len(fields) != 3 or fields[0] != uuid
                or fields[2] not in ("yes", "no")):
            return None
        ssid = self._nmcli_unescape(fields[1])
        return ssid if (1 <= len(ssid.encode("utf-8")) <= 32
                        and "\r" not in ssid and "\n" not in ssid) else None

    @staticmethod
    def _normalize_bssid(value: str | None) -> str:
        # `nmcli -g` escapes the colons in a MAC address by default
        # (AA\:BB\:...), while `iw` and our journal use plain colons.
        return Plugin._nmcli_unescape(str(value or "")).strip().lower()

    def _get_iwd_rank_modifier_snapshot(self) -> dict:
        """Read only the [Rank] band keys; other iwd settings may change while a
        policy is active, so the complete file is never stored or restored."""
        text, exists = "", True
        unsafe = os.path.islink(IWD_MAIN_CONF)
        if not unsafe:
            try:
                text = self._read_bounded_regular_text(IWD_MAIN_CONF)
            except FileNotFoundError:
                exists = False
            except (OSError, UnicodeError, ValueError, UnsafeSettingsPath):
                unsafe = True

        rank_count = 0
        counts = {prefix: 0 for prefix, _ in IWD_RANK_KEYS}
        values = {prefix: "" for prefix, _ in IWD_RANK_KEYS}
        section_re = re.compile(r"^\s*\[([^]]+)]\s*(?:[#;].*)?$")
        key_res = {prefix: re.compile(rf"^\s*{key}\s*=\s*([^#;]*?)\s*(?:[#;].*)?$")
                   for prefix, key in IWD_RANK_KEYS}
        in_rank = False
        for raw_line in text.splitlines():
            section_match = section_re.match(raw_line)
            if section_match:
                in_rank = section_match.group(1).strip() == "Rank"
                if in_rank:
                    rank_count += 1
                continue
            if in_rank:
                for prefix, key_re in key_res.items():
                    key_match = key_re.match(raw_line)
                    if key_match:
                        counts[prefix] += 1
                        if counts[prefix] == 1:
                            values[prefix] = key_match.group(1).strip()

        snapshot = {
            "file_exists": exists,
            # Only an otherwise empty file's bytes: enough to tell "absent" from
            # "present but empty" without snapshotting unrelated iwd settings.
            "empty_file_content": text if exists and not unsafe and not text.strip() else None,
            "rank_section_present": rank_count > 0,
            "rank_section_count": rank_count,
            "ambiguous": unsafe or rank_count > 1 or counts[""] > 1,
        }
        if unsafe:
            snapshot["unsafe_symlink"] = True
        # The 5/6 GHz keys are read-only safety inputs: this feature never
        # writes them, but iwd refuses a Rank config disabling 2.4 and 5 GHz.
        for prefix, count in counts.items():
            snapshot[f"{prefix}modifier_present"] = count > 0
            snapshot[f"{prefix}modifier_count"] = count
            snapshot[f"{prefix}modifier_value"] = values[prefix]
        return snapshot

    @staticmethod
    def _iwd_band_modifier_error(snapshot: dict, band: str) -> str:
        """Refuse a duplicate, non-numeric or non-positive 5 GHz ("five") or 6 GHz ("six") modifier."""
        key = "BandModifier5GHz" if band == "five" else "BandModifier6GHz"
        if int(snapshot.get(f"{band}_modifier_count") or 0) > 1:
            return f"The iwd config contains duplicate {key} keys."
        if not snapshot.get(f"{band}_modifier_present"):
            return ""
        try:
            value = float(str(snapshot.get(f"{band}_modifier_value") or "").strip())
        except (TypeError, ValueError):
            return f"The iwd {key} value is not numeric."
        if not math.isfinite(value) or value <= 0:
            return f"{key} must remain above zero before high-band preference can be enabled."
        return ""

    def _set_iwd_rank_modifier(
        self, value: str | None, cleanup_created_rank: bool = False
    ):
        """Set/remove `[Rank] BandModifier2_4GHz` without rewriting INI data."""
        # Restored values come from the deck-owned settings file; a line break would inject iwd config.
        if value is not None and any(ord(c) < 0x20 or c == "\x7f" for c in value):
            raise ValueError("Refusing an iwd band modifier value with control characters")
        if os.path.islink(IWD_MAIN_CONF):
            raise ValueError("Refusing to modify a symlinked iwd main.conf")
        try:
            original = self._read_bounded_regular_text(IWD_MAIN_CONF)
            file_existed = True
        except FileNotFoundError:
            original = ""
            file_existed = False

        current_snapshot = self._get_iwd_rank_modifier_snapshot()
        if current_snapshot.get("ambiguous"):
            raise ValueError(
                "Duplicate [Rank] sections or BandModifier2_4GHz keys in iwd config"
            )

        newline = "\r\n" if "\r\n" in original else "\n"
        lines = original.splitlines(keepends=True)
        section_re = re.compile(r"^\s*\[([^]]+)]\s*(?:[#;].*)?(?:\r?\n)?$")
        key_re = re.compile(r"^(\s*)BandModifier2_4GHz\s*=.*?(\r?\n)?$")

        rank_start = None
        rank_end = len(lines)
        key_index = None
        for index, raw_line in enumerate(lines):
            section_match = section_re.match(raw_line)
            if section_match:
                if rank_start is not None:
                    rank_end = index
                    break
                # iwd matches section names case-sensitively.
                if section_match.group(1).strip() == "Rank":
                    rank_start = index
                continue
            if rank_start is not None and key_index is None and key_re.match(raw_line):
                key_index = index

        if key_index is not None:
            if value is None:
                del lines[key_index]
                if key_index < rank_end:
                    rank_end -= 1
            else:
                match = key_re.match(lines[key_index])
                indent = match.group(1) if match else ""
                line_ending = match.group(2) if match and match.group(2) else newline
                lines[key_index] = f"{indent}BandModifier2_4GHz={value}{line_ending}"
        elif value is not None:
            if rank_start is None:
                if lines and not lines[-1].endswith(("\n", "\r")):
                    lines[-1] += newline
                if lines and lines[-1].strip():
                    lines.append(newline)
                lines.extend(
                    [f"[Rank]{newline}", f"BandModifier2_4GHz={value}{newline}"]
                )
                rank_start = len(lines) - 2
                rank_end = len(lines)
            else:
                if not lines[rank_end - 1].endswith(("\n", "\r")):
                    lines[rank_end - 1] += newline
                lines.insert(rank_end, f"BandModifier2_4GHz={value}{newline}")

        if value is None and cleanup_created_rank and rank_start is not None:
            # Remove a section header created by us only if it contains no
            # non-comment settings.  User comments are retained.
            rank_end = len(lines)
            for index in range(rank_start + 1, len(lines)):
                if section_re.match(lines[index]):
                    rank_end = index
                    break
            meaningful = [
                line
                for line in lines[rank_start + 1 : rank_end]
                if line.strip() and not line.lstrip().startswith(("#", ";"))
            ]
            comments = [
                line
                for line in lines[rank_start + 1 : rank_end]
                if line.strip() and line.lstrip().startswith(("#", ";"))
            ]
            if not meaningful and not comments:
                del lines[rank_start:rank_end]

        updated = "".join(lines)
        if updated == original:
            return
        if not file_existed and not updated.strip():
            return
        if not updated.strip():
            try:
                os.remove(IWD_MAIN_CONF)
            except FileNotFoundError:
                pass
            return
        self._atomic_write_text(IWD_MAIN_CONF, updated)

    def _restore_iwd_rank_modifier(self, snapshot: dict):
        before_present = bool(snapshot.get("modifier_present"))
        before_value = str(snapshot.get("modifier_value") or "")
        self._set_iwd_rank_modifier(
            before_value if before_present else None,
            cleanup_created_rank=not bool(snapshot.get("rank_section_present")),
        )
        if snapshot.get("file_exists"):
            empty_content = snapshot.get("empty_file_content")
            if empty_content is not None and not os.path.exists(IWD_MAIN_CONF):
                self._atomic_write_text(IWD_MAIN_CONF, str(empty_content))
        else:
            current = self._get_iwd_rank_modifier_snapshot()
            if not current.get("modifier_present"):
                try:
                    remaining = self._read_bounded_regular_text(IWD_MAIN_CONF)
                    if not remaining.strip():
                        os.remove(IWD_MAIN_CONF)
                except FileNotFoundError:
                    pass

    def _get_link_frequency(self, iface: str | None = None) -> int | None:
        iface = iface or self._get_wifi_interface()
        if not iface:
            return None
        result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"], timeout=3)
        for line in result.get("stdout", "").splitlines():
            match = re.match(r"\s*freq:\s*(\d+)", line)
            if match:
                return int(match.group(1))
        return None

    def _get_link_bssid(self, iface: str | None = None) -> str:
        iface = iface or self._get_wifi_interface()
        if not iface:
            return ""
        result = self._run_cmd(["/usr/bin/iw", "dev", iface, "link"], timeout=3)
        match = re.search(
            r"^Connected to\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})\b",
            result.get("stdout", ""),
            re.IGNORECASE | re.MULTILINE,
        )
        return match.group(1).lower() if match else ""

    def _get_enabled_high_band_frequencies(self) -> tuple[list[int], list[int]]:
        result = self._run_cmd(["/usr/bin/iw", "list"], timeout=6)
        frequencies: set[int] = set()
        for line in result.get("stdout", "").splitlines():
            if "disabled" in line.lower():
                continue
            match = re.search(r"\*\s*(\d+(?:\.\d+)?)\s+MHz", line)
            if match:
                frequencies.add(int(float(match.group(1))))
        return (
            sorted(freq for freq in frequencies if 4900 <= freq < 5925),
            sorted(freq for freq in frequencies if 5925 <= freq < 7125),
        )

    @staticmethod
    def _parse_mhz(value) -> int | None:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(?:MHz)?\s*", str(value or ""), re.IGNORECASE)
        try:
            return int(float(match.group(1))) if match else None
        except (OverflowError, ValueError):
            return None

    @staticmethod
    def _signal(value) -> float:
        try:
            return float(str(value).strip())
        except ValueError:
            return -999.0

    @staticmethod
    def _iw_scan_command(iface: str, ssid: str, frequencies=()) -> list[str]:
        scope = ["freq", *map(str, frequencies)] if frequencies else []
        return ["/usr/bin/iw", "dev", iface, "scan", *scope, "ssid", ssid]

    def _iw_scan_bss(self, stdout: str, ssid: str) -> list[dict]:
        """BSS records of `iw scan` output whose SSID is exactly `ssid`."""
        found = []
        bssid, frequency, signal = "", None, None
        for raw_line in stdout.splitlines():
            line = raw_line.lstrip()
            if line.startswith("BSS "):
                match = re.match(r"BSS\s+([0-9a-f]{2}(?::[0-9a-f]{2}){5})(?:\(|\s|$)", line, re.IGNORECASE)
                bssid, frequency, signal = match.group(1).lower() if match else "", None, None
            elif line.startswith("freq:"):
                frequency = self._parse_mhz(line.split(":", 1)[1])
            elif line.startswith("signal:"):
                match = re.match(r"signal:\s*(-?\d+(?:\.\d+)?)", line, re.IGNORECASE)
                signal = match.group(1) if match else None
            elif line.startswith("SSID:"):
                visible_ssid = line.split(":", 1)[1]
                if visible_ssid.startswith(" "):
                    visible_ssid = visible_ssid[1:]
                if self._iw_ssid(visible_ssid) == ssid:
                    found.append({"bssid": bssid, "frequency": frequency, "signal_dbm": self._signal(signal)})
        return found

    def _nmcli_wifi_bss(self, iface: str, ssid: str, fields: str, rescan: bool, timeout: int) -> list[dict] | None:
        """BSS records NetworkManager lists for `ssid`; None when the listing failed.

        `fields` is a comma-separated subset of BSSID,SSID,FREQ,SIGNAL.
        """
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "--escape", "yes", "-f", fields, "dev", "wifi", "list",
             "ifname", iface, "--rescan", "yes" if rescan else "no"],
            timeout=timeout,
        )
        if not result.get("success"):
            return None
        names = fields.split(",")
        found = []
        for line in result.get("stdout", "").split("\n"):
            values = self._nmcli_fields(line)
            if len(values) != len(names):
                continue
            row = dict(zip(names, values))
            if row["SSID"] == ssid:
                found.append({"bssid": row.get("BSSID", "").lower(), "frequency": self._parse_mhz(row["FREQ"]),
                              "signal_dbm": self._signal(row["SIGNAL"])})
        return found

    def _scan_high_band_candidates_for_active_ssid(
        self,
        iface: str | None,
        uuid: str | None,
        five_ghz_frequencies: list[int] | None = None,
        six_ghz_frequencies: list[int] | None = None,
        minimum_frequency: int = 4900,
    ) -> dict:
        """Return fresh, exact BSS candidates without changing the connection.

        The manual action needs a BSSID, not merely a band count; stop after
        the first successful scan that finds a high-band BSS.
        """
        result = {
            "scan_ok": False,
            "scan_source": "",
            "ssid": "",
            "candidates": [],
        }
        if not iface or not uuid:
            return result
        ssid = self._get_profile_ssid(uuid)
        if ssid is None:
            return result
        result["ssid"] = ssid
        candidates: dict[str, dict] = {}
        sources: list[str] = []

        def add_candidates(found: list[dict]):
            for candidate in found:
                bssid, freq = candidate["bssid"], candidate["frequency"]
                if not re.fullmatch(r"[0-9a-f]{2}(?::[0-9a-f]{2}){5}", bssid):
                    continue
                if freq is None or not minimum_frequency <= freq < 7125:
                    continue
                previous = candidates.get(bssid)
                if previous is None or candidate["signal_dbm"] > previous["signal_dbm"]:
                    candidates[bssid] = candidate

        def add_iw_scan(frequencies: list[int], source: str) -> bool:
            scan = self._run_cmd(self._iw_scan_command(iface, ssid, frequencies),
                                 timeout=10, preserve_stdout=True)
            if not scan.get("success"):
                return False
            result["scan_ok"] = True
            sources.append(source)
            add_candidates(self._iw_scan_bss(scan.get("stdout", ""), ssid))
            return True

        broad_ok = add_iw_scan([], "iw-directed")
        if not candidates:
            high_frequencies = sorted(
                freq for freq in set(five_ghz_frequencies or []) | set(six_ghz_frequencies or [])
                if minimum_frequency <= freq < 7125
            )
            if high_frequencies:
                add_iw_scan(high_frequencies, "iw-directed-high-band")

        # Portable fallback.  Do not ask NetworkManager for another active
        # scan if nl80211 already completed one; merge only its current view.
        if not candidates:
            found = self._nmcli_wifi_bss(iface, ssid, "BSSID,SSID,FREQ,SIGNAL",
                                         rescan=not broad_ok, timeout=5 if broad_ok else 10)
            if found is not None:
                result["scan_ok"] = True
                sources.append("nmcli")
                add_candidates(found)

        result["candidates"] = sorted(
            candidates.values(),
            key=lambda item: (
                item["signal_dbm"],
                1 if item["frequency"] >= 5925 else 0,
            ),
            reverse=True,
        )
        result["scan_source"] = "+".join(sources)
        return result

    def _get_visible_bands_for_active_ssid(
        self,
        iface: str | None,
        uuid: str | None,
        rescan: bool = True,
        five_ghz_frequencies: list[int] | None = None,
        six_ghz_frequencies: list[int] | None = None,
        allow_recent_cache: bool = True,
        need_five: bool = True,
    ) -> dict:
        bands = {
            "two_ghz": 0,
            "five_ghz": 0,
            "six_ghz": 0,
            "scan_ok": False,
            "scan_source": "",
            "fresh_high_band": False,
        }
        if not iface or not uuid:
            return bands
        ssid = self._get_profile_ssid(uuid)
        if ssid is None:
            return bands

        # A recheck in the UI is immediately followed by a separate backend
        # preflight when the user applies a mode. Wireless scans are snapshots
        # and an AP can be omitted from either one, especially on DFS/6 GHz.
        # Keep only a short, connection-scoped copy of a successful high-band
        # observation so the apply call can use the result it just presented.
        cache_ttl_seconds = 60.0
        now = time.monotonic()
        cached = getattr(self, "_visible_band_cache", None)
        cached_bands = None
        if (
            isinstance(cached, dict)
            and cached.get("iface") == iface
            and cached.get("uuid") == uuid
            and cached.get("ssid") == ssid
            and 0 <= now - float(cached.get("checked_at", 0)) <= cache_ttl_seconds
        ):
            cached_bands = cached.get("bands")

        sources = []

        def add_frequencies(found: list[dict], *, fresh: bool):
            for item in found:
                freq = item["frequency"]
                if freq is None:
                    continue
                if 2400 <= freq < 2500:
                    bands["two_ghz"] += 1
                elif 4900 <= freq < 7125:
                    bands["five_ghz" if freq < 5925 else "six_ghz"] += 1
                    if fresh:
                        bands["fresh_high_band"] = True

        # NetworkManager's iwd backend may expose one aggregate AP object per
        # SSID. On the Go 2 this can show only the currently associated 2.4 GHz
        # BSS. A passive nl80211 scan was also observed to omit the router's
        # DFS 5 GHz BSS at 5320 MHz. A directed scan for the already-connected
        # SSID exposes its separate 2.4, 5 and 6 GHz BSS records without fixing
        # a BSSID or changing the saved connection profile.
        def add_direct_scan(frequencies: list[int], source: str) -> bool:
            direct_result = self._run_cmd(self._iw_scan_command(iface, ssid, frequencies),
                                          timeout=15, preserve_stdout=True)
            if not direct_result.get("success"):
                return False
            bands["scan_ok"] = True
            if source not in sources:
                sources.append(source)
            add_frequencies(self._iw_scan_bss(direct_result.get("stdout", ""), ssid), fresh=True)
            return True

        direct_scan_ok = False
        if rescan:
            direct_scan_ok = add_direct_scan([], "iw-directed")
            # DFS 5 GHz records can be absent from a successful all-band scan.
            # A second scan restricted to the radio's enabled 5 GHz channels
            # reliably exposed the missing BSS on the tested Go 2. Do the same
            # for 6 GHz only when that band was absent from the first result.
            for frequencies_for_band, band_key, source in (
                (five_ghz_frequencies, "five_ghz", "iw-directed-5ghz"),
                (six_ghz_frequencies, "six_ghz", "iw-directed-6ghz"),
            ):
                # Only 6 GHz decides on 6 GHz alone: 5 GHz searches would only hold the queue.
                if bands[band_key] == 0 and frequencies_for_band and (need_five or band_key != "five_ghz"):
                    direct_scan_ok = add_direct_scan(frequencies_for_band, source) or direct_scan_ok

            # A many-channel request can still dwell too briefly on DFS. Scan
            # each enabled DFS frequency individually until the SSID is found.
            # On the live router this stops at 5320 MHz (channel 64), normally
            # adding under two seconds rather than another broad scan.
            if need_five and bands["five_ghz"] == 0 and five_ghz_frequencies:
                for freq in five_ghz_frequencies:
                    if not 5260 <= freq <= 5720:
                        continue
                    direct_scan_ok = add_direct_scan([freq], "iw-directed-5ghz-dfs") or direct_scan_ok
                    if bands["five_ghz"] > 0:
                        break

        # Keep nmcli as a portable fallback and merge its cached results. Do
        # not trigger a second active scan when nl80211 already completed one.
        nmcli_rescan = bool(rescan and not direct_scan_ok)
        found = self._nmcli_wifi_bss(iface, ssid, "FREQ,SSID,SIGNAL",
                                     rescan=nmcli_rescan, timeout=12 if nmcli_rescan else 4)
        if found is not None:
            bands["scan_ok"] = True
            sources.append("nmcli")
            add_frequencies(found, fresh=nmcli_rescan)

        fresh_high_band = bands["five_ghz"] > 0 or bands["six_ghz"] > 0
        if bands["scan_ok"] and fresh_high_band:
            self._visible_band_cache = {
                "iface": iface,
                "uuid": uuid,
                "ssid": ssid,
                "checked_at": now,
                "bands": {
                    "two_ghz": bands["two_ghz"],
                    "five_ghz": bands["five_ghz"],
                    "six_ghz": bands["six_ghz"],
                },
            }
        elif allow_recent_cache and isinstance(cached_bands, dict):
            for key in ("two_ghz", "five_ghz", "six_ghz"):
                bands[key] = max(bands[key], int(cached_bands.get(key, 0)))
            sources.append("recent-cache")
        bands["scan_source"] = "+".join(sources)
        return bands

    def _nm_supports_six_ghz_only(self) -> bool:
        """The per-profile 6 GHz band needs NetworkManager 1.58+. Cached once read."""
        if getattr(self, "_nm_six_ghz_supported", None) is None:
            result = self._run_cmd(["/usr/bin/nmcli", "--version"], timeout=3)
            version = self._parse_version(result.get("stdout", ""))
            if not version:
                return False
            self._nm_six_ghz_supported = version >= (1, 58, 0)
        return self._nm_six_ghz_supported

    def _get_band_policy_capabilities_sync(self, rescan: bool = True, six_only: bool = False) -> dict:
        iface = self._get_wifi_interface()
        uuid = self._get_active_connection_uuid(iface) if iface else None
        backend = self._get_current_backend() or ""
        _, device_family, _ = self._detect_device_family()
        driver = self._detect_wifi_driver(iface)
        target_supported = device_family == "legion_go_2" and driver == "mt7921e"

        nm_supports_6ghz = self._nm_supports_six_ghz_only()

        iwd_version_result = self._run_cmd(
            ["/usr/lib/iwd/iwd", "--version"], timeout=3
        )
        iwd_service = self._run_cmd(
            ["/usr/bin/systemctl", "is-active", "iwd"], timeout=3
        )
        iwd_active = iwd_service.get("stdout", "").strip() == "active"
        iwd_available = iwd_version_result.get("success", False) or os.path.isfile(
            "/usr/lib/systemd/system/iwd.service"
        )

        five_ghz, six_ghz = self._get_enabled_high_band_frequencies()
        has_5ghz, has_6ghz = bool(five_ghz), bool(six_ghz)

        visible = self._get_visible_bands_for_active_ssid(
            iface,
            uuid,
            rescan and target_supported,
            five_ghz_frequencies=five_ghz,
            six_ghz_frequencies=six_ghz,
            need_five=not six_only,
        )
        six_visible = visible["six_ghz"] > 0
        high_visible = visible["five_ghz"] > 0 or six_visible

        band = ""
        channel = ""
        bssid = ""
        if uuid:
            band, _ = self._nmcli_get(uuid, "802-11-wireless.band")
            channel, _ = self._nmcli_get(uuid, "802-11-wireless.channel")
            bssid, _ = self._nmcli_get(uuid, "802-11-wireless.bssid")

        settings = _load_settings()
        current_policy = settings.get("band_policy", BAND_POLICY_OFF)
        policy_state = settings.get("band_policy_state") or {}
        applied_state = policy_state.get("applied") or {}
        iwd_snapshot = self._get_iwd_rank_modifier_snapshot()
        five_modifier_error = self._iwd_band_modifier_error(iwd_snapshot, "five")
        six_modifier_error = self._iwd_band_modifier_error(iwd_snapshot, "six")
        external_band_while_off = current_policy == BAND_POLICY_OFF and bool(band)
        external_iwd_while_off = (
            current_policy == BAND_POLICY_OFF
            and iwd_snapshot.get("modifier_present")
        )
        external_band_during_high = (
            current_policy in IWD_BAND_POLICIES
            and bool(band)
        )
        external_iwd_during_six = (
            current_policy == BAND_POLICY_SIX_ONLY
            and not self._iwd_snapshot_matches(iwd_snapshot, self._applied_iwd(applied_state))
        )
        fixed_channel = channel not in ("", "0")
        fixed_bssid = bool(bssid)
        watchdog_available = os.path.isfile("/usr/bin/systemd-run") and os.path.isfile(
            "/usr/bin/python3"
        )

        common_reason = ""
        if not iface or not uuid:
            common_reason = "Connect to WiFi first."
        elif not target_supported:
            common_reason = (
                "Band changes are enabled only on a Legion Go 2 with the "
                "MediaTek MT7922 (mt7921e) driver."
            )
        elif fixed_bssid:
            common_reason = "Clear the fixed BSSID before changing band policy."
        elif fixed_channel:
            common_reason = "Clear the fixed WiFi channel before changing band policy."
        elif iwd_snapshot.get("unsafe_symlink"):
            common_reason = "Refusing a symlinked /etc/iwd/main.conf."
        elif iwd_snapshot.get("ambiguous"):
            common_reason = (
                "The iwd config contains duplicate [Rank] sections or band keys."
            )
        elif not watchdog_available:
            common_reason = "The automatic rollback watchdog is unavailable."

        reason_six = common_reason
        if not reason_six and backend == "iwd":
            reason_six = (
                "iwd does not enforce NetworkManager's per-profile band setting. "
                "Use the iwd 5/6 GHz policy instead."
            )
        elif not reason_six and backend != "wpa_supplicant":
            reason_six = "The WiFi backend cannot enforce a per-profile band setting."
        elif not reason_six and not nm_supports_6ghz:
            reason_six = "NetworkManager 1.58 or newer is required for 6 GHz only."
        elif not reason_six and not has_6ghz:
            reason_six = "No usable 6 GHz channels are exposed by the radio and regdomain."
        elif not reason_six and six_modifier_error:
            reason_six = six_modifier_error
        elif not reason_six and (external_band_while_off or external_band_during_high):
            reason_six = "The active profile already has an external band setting."
        elif not reason_six and external_iwd_while_off:
            reason_six = "An external iwd band policy is already configured."
        elif not reason_six and not six_visible:
            reason_six = "No 6 GHz BSS for the active SSID is currently visible."

        reason_high = common_reason
        if not reason_high and backend != "iwd":
            reason_high = "The 5/6 GHz policy requires the iwd WiFi backend."
        elif not reason_high and not iwd_available:
            reason_high = "iwd is not installed."
        elif not reason_high and not iwd_active:
            reason_high = "The iwd service is not active."
        elif not reason_high and not (has_5ghz or has_6ghz):
            reason_high = "No usable 5 or 6 GHz channels are exposed by the radio."
        elif not reason_high and five_modifier_error:
            reason_high = five_modifier_error
        elif not reason_high and six_modifier_error:
            reason_high = six_modifier_error
        elif not reason_high and external_band_while_off:
            reason_high = "The active profile already has an external band setting."
        elif not reason_high and (
            external_iwd_while_off or external_iwd_during_six
        ):
            reason_high = "An external iwd band policy is already configured."
        if not reason_high and uuid:
            reason_high = self._get_iwd_profile_conflict(uuid, allow_disabled_duplicates=True)
        reason_high_hard = reason_high
        if not reason_high and not high_visible:
            reason_high = "No 5 or 6 GHz BSS for the active SSID is currently visible."

        # The preference may tolerate a short scan cache because 2.4 GHz
        # remains a fallback. The strict policy must use a fresh observation;
        # a cached BSS could be out of range by the time 2.4 GHz is disabled.
        reason_strict_high = reason_high
        if not reason_strict_high and not visible["fresh_high_band"]:
            reason_strict_high = (
                "No fresh 5 or 6 GHz scan result for the active SSID was verified."
            )

        return {
            "success": True,
            "six_ghz_only_available": not bool(reason_six),
            "five_six_no_24_available": not bool(reason_high),
            "five_six_only_available": not bool(reason_strict_high),
            "five_six_only_try_available": not bool(reason_high_hard),
            "reason_six_ghz_only": reason_six,
            "reason_five_six_no_24": reason_high,
            "reason_five_six_only": reason_strict_high,
            "reason_five_six_only_try": reason_high_hard,
        }

    def _load_band_policy_journal(self, path: str | None = None) -> dict | None:
        target = path or BAND_POLICY_JOURNAL_FILE
        try:
            return load_json_object(target)
        except FileNotFoundError:
            return None
        except (CorruptSettings, UnsafeSettingsPath, OSError) as exc:
            # A present but unreadable recovery journal is materially
            # different from no journal. Return a fail-closed marker so every
            # mutating path remains locked until the file is inspected.
            diagnostic_transition('wifi', 'recovery_journal', 'unreadable', level='error',
                                  code=type(exc).__name__, errno=getattr(exc, 'errno', None),
                                  store='band_policy')
            return {
                "schema": 0,
                "phase": "unreadable",
                "rollback_conflicts": [str(exc)],
            }

    def _save_band_policy_journal(self, journal: dict, path: str | None = None):
        target = path or BAND_POLICY_JOURNAL_FILE
        directory = os.path.dirname(target)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        info = os.lstat(directory)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise UnsafeSettingsPath("Recovery directory is not a real directory")
        if os.name == "posix" and info.st_uid != os.geteuid():
            raise UnsafeSettingsPath("Recovery directory has an unexpected owner")
        os.chmod(directory, 0o700)
        self._atomic_write_json(target, journal)
        phase = journal.get('phase')
        phase = phase if isinstance(phase, str) and phase in JOURNAL_PHASES else 'unknown'
        store = 'band_policy'
        operation = journal.get('operation')
        operation = operation if isinstance(operation, str) and operation in {'manual_high_band_reconnect', 'automatic_high_band_reconnect'} else store
        diagnostic_transition('wifi', 'transaction', operation + ':' + phase,
                              level='warning' if phase == 'rollback_failed' else 'info',
                              operation=operation, phase=phase, store=store,
                              pending=phase not in {'committed', 'rolled_back'},
                              restored=phase == 'rolled_back')

    def _remove_band_policy_journal(self, path: str | None = None):
        try:
            target = path or BAND_POLICY_JOURNAL_FILE
            os.remove(target)
            if os.name == "posix":
                fd = os.open(os.path.dirname(target), os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        except FileNotFoundError:
            pass

    def _get_network_recovery_issues(self) -> list[str]:
        """Describe only durable recovery failures, without mutating state."""
        journal = self._load_band_policy_journal()
        if not journal:
            return []
        phase = str(journal.get("phase") or "")
        if phase == "rollback_failed":
            conflicts = journal.get("rollback_conflicts") or []
            detail = "; ".join(str(item) for item in conflicts)
            return ["band policy rollback failed" + (f": {detail}" if detail else "")]
        if phase not in JOURNAL_PHASES:
            return [f"band policy journal has unknown phase: {phase or 'missing'}"]
        return []

    def _write_band_settings_fields(self, path: str, fields: dict):
        """Merge only the supplied owned fields, preserving concurrent settings."""
        if os.path.abspath(path) == os.path.abspath(SETTINGS_FILE):
            data = _load_settings()
        else:
            data = load_json_object(path, missing_ok=True) or {}
        data.update(fields)
        data["last_applied"] = int(time.time())
        if os.path.abspath(path) == os.path.abspath(SETTINGS_FILE):
            _save_settings(data)
        else:
            atomic_write_json(path, data)

    @staticmethod
    def _band_settings_fields(settings: dict) -> dict:
        return {
            "band_policy": settings.get("band_policy", BAND_POLICY_OFF),
            "band_policy_state": settings.get("band_policy_state", {}),
            "band_preference_enabled": bool(
                settings.get("band_preference_enabled")
            ),
        }

    def _schedule_band_policy_rollback(self, journal: dict) -> dict:
        unit = (
            f"{BAND_POLICY_ROLLBACK_UNIT_PREFIX}-{os.getpid()}-"
            f"{int(time.time() * 1000)}"
        )
        journal["rollback_unit"] = unit
        self._save_band_policy_journal(journal)
        main_path = os.path.join(DECKY_PLUGIN_DIR, "wifi_backend.py")
        try:
            rollback_seconds = int(
                journal.get("rollback_delay_seconds", BAND_POLICY_ROLLBACK_SECONDS)
            )
        except (TypeError, ValueError):
            rollback_seconds = BAND_POLICY_ROLLBACK_SECONDS
        rollback_seconds = min(
            BAND_POLICY_ROLLBACK_SECONDS, max(30, rollback_seconds)
        )
        result = self._run_cmd(
            [
                "/usr/bin/systemd-run",
                "--quiet",
                "--collect",
                f"--unit={unit}",
                f"--on-active={rollback_seconds}s",
                "--timer-property=AccuracySec=1s",
                "/usr/bin/python3",
                main_path,
                "--rollback-band-policy",
                BAND_POLICY_JOURNAL_FILE,
            ],
            timeout=8,
        )
        return result

    def _cancel_band_policy_rollback(self, journal: dict):
        unit = str(journal.get("rollback_unit") or "")
        if not unit:
            return
        for suffix in ("timer", "service"):
            self._run_cmd(["/usr/bin/systemctl", "stop", f"{unit}.{suffix}"], timeout=4)
        self._run_cmd(["/usr/bin/systemctl", "reset-failed", f"{unit}.service"], timeout=4)

    def _profile_is_active(self, uuid: str, iface: str | None = None) -> bool:
        if not uuid:
            return False
        iface = iface or self._get_wifi_interface()
        if not iface:
            return False
        # --active also includes ACTIVATING profiles and other adapters.
        # Read state and UUID together for the interface whose iw link we use.
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-g", "GENERAL.STATE,GENERAL.CON-UUID", "device", "show", iface]
        )
        lines = result.get("stdout", "").splitlines()
        return bool(result.get("success") and len(lines) == 2
                    and re.fullmatch(r"100(?:\s+\(connected\))?", lines[0].strip())
                    and lines[1].strip() == uuid)

    def _get_iwd_profile_conflict(self, uuid: str, *, allow_disabled_duplicates: bool = False) -> str:
        """Reject ambiguous saved PSK/SAE profiles without reading credentials.

        iwd keeps one KnownNetwork per SSID/security family, while NM may hold
        several UUIDs with different credentials for it. Only an iwd restart
        rebuilds NM's mirror binding, so only that path may allow disabled
        duplicates when the target is the sole public metadata match.
        """
        unavailable = "Saved WiFi profiles could not be verified before restarting iwd."
        listed = self._run_cmd([
            "/usr/bin/nmcli", "-t", "-f", "UUID,TYPE", "con", "show",
        ], timeout=4)
        if not listed.get("success"):
            return unavailable
        uuids = []
        for line in listed.get("stdout", "").splitlines():
            fields = line.split(":")
            if len(fields) != 2:
                return unavailable
            if fields[1] == "802-11-wireless":
                if not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", fields[0]):
                    return unavailable
                uuids.append(fields[0])
        if uuid not in uuids or len(uuids) > 32 or len(set(uuids)) != len(uuids):
            return unavailable
        # `-t -f` prints "key:value" lines and omits settings a profile lacks
        # (an open network has no 802-11-wireless-security), so parse by key.
        keys = ("connection.uuid", "connection.type", "connection.autoconnect",
                "802-11-wireless.ssid", "802-11-wireless.hidden",
                "802-11-wireless-security.key-mgmt")
        args = ["/usr/bin/nmcli", "-t", "-f", ",".join(keys), "con", "show"]
        for saved_uuid in uuids:
            args.extend(["uuid", saved_uuid])
        metadata = self._run_cmd(args, timeout=6)
        if not metadata.get("success"):
            return unavailable
        records = [{}]
        for line in metadata.get("stdout", "").split("\n"):
            if not line:
                if records[-1]:
                    records.append({})
                continue
            key, _, value = line.partition(":")
            if key not in keys or key in records[-1]:
                return unavailable
            records[-1][key] = value
        profiles = {}
        for record in records:
            if not record:
                continue
            saved_uuid, kind, auto, ssid, hidden, security = (record.get(key, "") for key in keys)
            if saved_uuid not in uuids or saved_uuid in profiles or kind != "802-11-wireless":
                return unavailable
            if auto not in ("yes", "no") or hidden not in ("yes", "no"):
                return unavailable
            if "\r" in ssid:
                return unavailable
            profiles[saved_uuid] = (self._nmcli_unescape(ssid), security, auto == "yes", hidden == "yes")
        if set(profiles) != set(uuids):
            return unavailable
        ssid, security, auto, hidden = profiles[uuid]
        if not ssid or ssid == "--":
            return unavailable
        if security not in ("sae", "wpa-psk"):
            return ""
        siblings = {uid: fields for uid, fields in profiles.items()
                    if fields[0] == ssid and fields[1] in ("sae", "wpa-psk")}
        if len(siblings) > 1:
            preferences = (self._get_iwd_known_network_preferences(ssid)
                           if allow_disabled_duplicates else None)
            if auto and preferences == (True, hidden):
                candidates = [uid for uid, fields in siblings.items()
                              if fields[2:] == preferences]
                if candidates == [uuid]:
                    return ""
            return (
                "Multiple saved WiFi profiles share this SSID's iwd PSK/SAE network. "
                "Select one matching autoconnect profile before changing the band policy."
            )
        return ""

    def _get_iwd_known_network_preferences(self, ssid: str) -> tuple[bool, bool] | None:
        """Read only public metadata used by NM to select an iwd mirror."""
        result = self._run_cmd([
            "/usr/bin/busctl", "--json=short", "call", "net.connman.iwd", "/",
            "org.freedesktop.DBus.ObjectManager", "GetManagedObjects",
        ], timeout=4)
        if not result.get("success"):
            return None
        try:
            objects = json.loads(result.get("stdout", ""))["data"][0]
            if not isinstance(objects, dict):
                return None
            matches = []
            for interfaces in objects.values():
                known = interfaces.get("net.connman.iwd.KnownNetwork", {})
                if known.get("Name", {}).get("data") != ssid:
                    continue
                if known.get("Type", {}).get("data") != "psk":
                    continue
                auto = known.get("AutoConnect", {}).get("data")
                # NM treats the optional Hidden property as false if absent.
                hidden = known.get("Hidden", {"data": False}).get("data")
                if type(auto) is not bool or type(hidden) is not bool:
                    return None
                matches.append((auto, hidden))
            return matches[0] if len(matches) == 1 else None
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            return None

    def _reconnect_profile(
        self, uuid: str, was_active: bool = True, cycle: bool = True,
        activation_timeout: int = 25,
        bound_iface: str | None = None,
    ) -> dict:
        if not uuid or not was_active:
            return {"success": True, "reconnected": False}

        def active():
            return self._profile_is_active(uuid, bound_iface) if bound_iface else self._profile_is_active(uuid)
        def frequency():
            return self._get_link_frequency(bound_iface) if bound_iface else self._get_link_frequency()

        if not cycle and active() and frequency() is not None:
            return {"success": True, "reconnected": True, "autoconnected": True}
        if self._get_current_backend() == "iwd":
            conflict = self._get_iwd_profile_conflict(uuid, allow_disabled_duplicates=not cycle)
            if conflict:
                return {"success": False, "reconnected": False,
                        "error": "ambiguous_iwd_profiles", "stderr": conflict}

        # Restarting iwd already cycles the device. NetworkManager briefly has
        # no WiFi device at that point and an immediate `con up` can be matched
        # against loopback, producing "No suitable device". Give normal
        # autoconnect a short chance first, then wait for the real WiFi device
        # to become usable and bind any explicit activation to that interface.
        if cycle:
            down_result = self._run_cmd(
                ["/usr/bin/nmcli", "con", "down", "uuid", uuid], timeout=8
            )
            # `con down` fails on a profile that is not active (the state after
            # a failed activation); only a profile that stays up is an error.
            if not down_result.get("success") and active():
                return {
                    **down_result,
                    "reconnected": False,
                    "connection_active": True,
                }

        started = time.monotonic()
        deadline = started + max(15.0, min(25.0, float(activation_timeout)))
        # After an iwd restart, a DFS BSS can appear several seconds after the
        # device becomes available. Let iwd finish its own autoconnect before
        # asking NetworkManager to activate the profile manually.
        next_manual_activation = started if cycle else started + 8.0
        saw_inactive = not cycle
        last_result = None

        while time.monotonic() < deadline:
            if active():
                if (not cycle or saw_inactive) and frequency() is not None:
                    return {
                        "success": True,
                        "stdout": "",
                        "stderr": "",
                        "returncode": 0,
                        "reconnected": True,
                        "autoconnected": cycle,
                    }
                time.sleep(0.25)
                continue
            else:
                saw_inactive = True

            if time.monotonic() < next_manual_activation:
                time.sleep(0.25)
                continue

            iface = bound_iface or self._get_wifi_interface()
            if iface:
                state_result = self._run_cmd(
                    ["/usr/bin/nmcli", "-g", "GENERAL.STATE", "device", "show", iface], timeout=3)
                state_match = re.match(
                    r"\s*(\d+)", str(state_result.get("stdout") or "")
                )
                state = int(state_match.group(1)) if state_match else None
                # 30 is disconnected and 100 is connected. Intermediate
                # states mean an automatic activation is already underway, so
                # let it finish instead of racing a second activation.
                remaining_activation = deadline - time.monotonic() - 3.0
                if state_result.get("success") and state in (30, 100) and remaining_activation > 0:
                    last_result = self._run_cmd(
                        ["/usr/bin/nmcli", "con", "up", "uuid", uuid, "ifname", iface],
                        # Keep time for native iwd recovery after the CLI
                        # exits. Its error can precede a successful fallback.
                        timeout=min(activation_timeout, remaining_activation),
                    )
                    # A fast failure does not mean iwd has finished discovering
                    # the target AP (notably on passive/DFS channels). Repeated
                    # activations every 250 ms can race its background scan.
                    next_manual_activation = time.monotonic() + 3.0
                    if (last_result.get("success") and active()
                            and self._get_link_frequency(iface) is not None):
                        last_result["reconnected"] = True
                        return last_result

            time.sleep(0.25)

        verified = bool(active() and frequency() is not None)
        if verified:
            return {"success": True, "stdout": "", "stderr": "", "returncode": 0,
                    "reconnected": True, "autoconnected": True}
        if last_result is None:
            last_result = {
                "success": False,
                "stdout": "",
                "stderr": "WiFi device did not become ready before timeout.",
                "returncode": 1,
            }
        elif last_result.get("success"):
            last_result = {
                **last_result,
                "success": False,
                "stderr": "The requested WiFi profile did not obtain a radio link before timeout.",
                "returncode": 1,
            }
        last_result["reconnected"] = False
        return last_result

    def _wait_for_profile_frequency(
        self, uuid: str, timeout: float = 8.0
    ) -> int | None:
        deadline = time.monotonic() + timeout
        last_frequency = None
        while time.monotonic() < deadline:
            if self._profile_is_active(uuid):
                iface = self._get_wifi_interface()
                last_frequency = self._get_link_frequency(iface)
                if last_frequency is not None:
                    return last_frequency
            time.sleep(0.25)
        return last_frequency

    def _rescan_and_reconnect_sync(self, target_band: str = "high") -> dict:
        """Temporarily select one freshly seen 5/6 GHz BSS, then unpin it."""
        if target_band not in ("high", "six"):
            return {"success": False, "error": "invalid_target_band",
                    "message": "Choose 5/6 GHz or 6 GHz for this connection attempt."}
        # Like set_band_policy: finish a journal left behind (e.g. 'rolled_back'
        # by the watchdog) before refusing; the band-policy file lock is held.
        if self._load_band_policy_journal() and (
                not self._recover_band_policy_journal().get("success")
                or self._load_band_policy_journal()):
            return {
                "success": False,
                "error": "network_recovery_required",
                "message": "A previous network operation still needs recovery.",
                "recovery_state_preserved": True,
            }

        settings = _load_settings()
        backend = self._get_current_backend()
        mode = settings.get("band_policy", BAND_POLICY_OFF)
        if not (mode == BAND_POLICY_HIGH_ONLY and backend == "iwd"
                or mode == BAND_POLICY_OFF and backend == "wpa_supplicant"):
            return {
                "success": False,
                "error": "preference_disabled",
                "message": "Use the iwd preference or Automatic WiFi with wpa_supplicant before reconnecting.",
            }
        ownership_error = self._band_policy_ownership_error(settings)
        if ownership_error:
            return {
                "success": False,
                "error": "ownership_conflict",
                "message": ownership_error,
                "recovery_state_preserved": True,
            }

        _, device_family, _ = self._detect_device_family()
        driver = self._detect_wifi_driver()
        if device_family != "legion_go_2" or driver != "mt7921e":
            return {
                "success": False,
                "error": "unsupported_device",
                "message": (
                    "Manual high-band reconnect is enabled only on a "
                    "Legion Go 2 with the MediaTek MT7922 driver."
                ),
            }
        backend_service = self._run_cmd(
            ["/usr/bin/systemctl", "is-active", backend], timeout=3
        )
        if backend_service.get("stdout", "").strip() != "active":
            return {
                "success": False,
                "error": "backend_inactive",
                "message": "The selected WiFi backend is not active.",
            }

        iface = self._get_wifi_interface()
        uuid = self._get_active_connection_uuid(iface, require_connected=True) if iface else None
        if not iface or not uuid:
            return {
                "success": False,
                "error": "no_wifi",
                "message": "Connect to WiFi first, then retry.",
            }
        frequency_before = self._get_link_frequency(iface)
        minimum_frequency = 5925 if target_band == "six" else 4900
        if frequency_before is not None and minimum_frequency <= frequency_before < 7125:
            return {
                "success": True,
                "reconnected": False,
                "better_band_found": True,
                "frequency_before": frequency_before,
                "frequency": frequency_before,
                "message": "WiFi is already connected on the requested band.",
            }

        conflict = self._get_iwd_profile_conflict(uuid) if backend == "iwd" else ""
        if conflict:
            return {"success": False, "error": "ambiguous_iwd_profiles", "message": conflict}

        bssid_before, bssid_result = self._nmcli_get(
            uuid, "802-11-wireless.bssid"
        )
        if not bssid_result.get("success"):
            return {
                "success": False,
                "error": "profile_read_failed",
                "message": "The saved WiFi profile could not be read.",
                "detail": bssid_result.get("stderr", ""),
            }
        if bssid_before:
            return {
                "success": False,
                "error": "external_bssid_configuration",
                "message": (
                    "The WiFi profile already has a fixed BSSID. Companion did "
                    "not replace that setting."
                ),
            }

        band_before, band_result = self._nmcli_get(uuid, "802-11-wireless.band")
        if not band_result.get("success") or band_before:
            return {"success": False, "error": "external_band_configuration",
                    "message": "Clear the saved profile band restriction before a one-time reconnect."}

        five_frequencies, six_frequencies = (
            self._get_enabled_high_band_frequencies()
        )
        scan = self._scan_high_band_candidates_for_active_ssid(
            iface,
            uuid,
            five_ghz_frequencies=five_frequencies,
            six_ghz_frequencies=six_frequencies,
            minimum_frequency=minimum_frequency,
        )
        if not scan.get("scan_ok"):
            return {
                "success": False,
                "error": "scan_failed",
                "message": "The WiFi scan did not complete; the connection was not changed.",
                "reconnected": False,
                "frequency_before": frequency_before,
            }
        candidates = [candidate for candidate in scan.get("candidates", [])
                      if minimum_frequency <= int(candidate.get("frequency") or 0) < 7125]
        if not candidates:
            return {
                "success": True,
                "reconnected": False,
                "better_band_found": False,
                "frequency_before": frequency_before,
                "frequency": frequency_before,
                "message": "No access point on the requested band was found for this network.",
            }
        if self._get_active_connection_uuid(iface, require_connected=True) != uuid:
            return {"success": False, "error": "connection_changed",
                    "message": "The active WiFi connection changed while scanning."}
        for field, expected in (("802-11-wireless.bssid", bssid_before),
                                ("802-11-wireless.band", band_before)):
            value, observed = self._nmcli_get(uuid, field)
            if not observed.get("success") or value != expected:
                return {"success": False, "error": "profile_changed",
                        "message": "The WiFi profile changed while scanning; its settings were preserved."}
        selected = candidates[0]
        selected_bssid = self._normalize_bssid(selected.get("bssid"))
        selected_frequency = int(selected.get("frequency") or 0)
        settings_before = self._band_settings_fields(settings)
        operation_id = f"manual-{os.getpid()}-{int(time.time() * 1000)}"
        journal = {
            "schema": 1,
            "operation_id": operation_id,
            "operation": "manual_high_band_reconnect",
            "phase": "pending",
            "created_at": int(time.time()),
            "rollback_delay_seconds": MANUAL_RECONNECT_ROLLBACK_SECONDS,
            "settings_file": SETTINGS_FILE,
            "settings_before": settings_before,
            "settings_after": settings_before,
            "mode_before": mode,
            "mode_target": mode,
            "profile_before": {
                "uuid": uuid,
                "band": "",
                "bssid": bssid_before,
                "was_active": True,
            },
            "active_uuid_before": uuid,
            "iwd_before": self._get_iwd_rank_modifier_snapshot(),
            "rollback_reconnect_required": True,
            "rollback_reconnect_uuid": uuid,
            "mutations": {
                "nm_bssid": {
                    "planned": True,
                    "expected": selected_bssid,
                    "applied": False,
                }
            },
        }
        self._save_band_policy_journal(journal)
        watchdog = self._schedule_band_policy_rollback(journal)
        if not watchdog.get("success"):
            self._remove_band_policy_journal()
            return {
                "success": False,
                "error": "rollback_watchdog_failed",
                "message": "Automatic rollback could not be armed; no changes were made.",
                "detail": watchdog.get("stderr", ""),
            }

        try:
            journal["phase"] = "applying"
            self._save_band_policy_journal(journal)
            modified = self._nmcli_modify(
                uuid, "802-11-wireless.bssid", selected_bssid, timeout=8
            )
            if not modified.get("success"):
                raise RuntimeError(
                    modified.get("stderr")
                    or "NetworkManager rejected the temporary access-point selection"
                )
            observed_bssid, observed_result = self._nmcli_get(
                uuid, "802-11-wireless.bssid"
            )
            if (
                not observed_result.get("success")
                or self._normalize_bssid(observed_bssid) != selected_bssid
            ):
                raise RuntimeError(
                    "The temporary access-point selection could not be verified"
                )
            journal["mutations"]["nm_bssid"]["applied"] = True
            self._save_band_policy_journal(journal)

            reconnect = self._reconnect_profile(uuid, True, cycle=True, bound_iface=iface)
            if not reconnect.get("success"):
                raise RuntimeError(
                    reconnect.get("stderr")
                    or "WiFi could not connect to the selected 5/6 GHz access point"
                )
            frequency = self._wait_for_profile_frequency(uuid)
            connected_bssid = self._get_link_bssid(iface)
            if not (
                frequency is not None
                and minimum_frequency <= frequency < 7125
                and self._profile_is_active(uuid, iface)
                and connected_bssid == selected_bssid
            ):
                raise RuntimeError(
                    "The selected 5/6 GHz access point did not become the active link"
                )

            # The BSSID is needed only to make this one choice deterministic.
            # Clear it while the verified link stays up, preserving roaming and
            # the 2.4 GHz fallback for future connectivity.
            current_pin, current_pin_result = self._nmcli_get(uuid, "802-11-wireless.bssid")
            if not current_pin_result.get("success") or self._normalize_bssid(current_pin) != selected_bssid:
                raise RuntimeError("The temporary selection was changed outside Companion")
            cleared = self._nmcli_modify(
                uuid, "802-11-wireless.bssid", bssid_before, timeout=8
            )
            if not cleared.get("success"):
                raise RuntimeError(
                    cleared.get("stderr")
                    or "The temporary access-point selection could not be cleared"
                )
            final_bssid, final_result = self._nmcli_get(
                uuid, "802-11-wireless.bssid"
            )
            if (
                not final_result.get("success")
                or self._normalize_bssid(final_bssid)
                != self._normalize_bssid(bssid_before)
            ):
                raise RuntimeError(
                    "The saved WiFi profile still contains the temporary BSSID"
                )
            final_frequency = self._get_link_frequency(iface)
            if (not self._profile_is_active(uuid, iface) or final_frequency is None
                    or not minimum_frequency <= final_frequency < 7125
                    or self._get_link_bssid(iface) != selected_bssid):
                raise RuntimeError("The selected radio link changed while clearing the temporary selection")
            frequency = final_frequency

            journal["rollback_reconnect_completed"] = True
            journal["phase"] = "verified"
            journal["verified_frequency"] = frequency
            self._save_band_policy_journal(journal)
            journal["phase"] = "committed"
            self._save_band_policy_journal(journal)
            self._cancel_band_policy_rollback(journal)
            self._remove_band_policy_journal()
            return {
                "success": True,
                "reconnected": True,
                "better_band_found": True,
                "attempts": 1,
                "frequency_before": frequency_before,
                "frequency": frequency,
                "selected_frequency": selected_frequency,
                "temporary_bssid_cleared": True,
                "message": "Reconnected on 5/6 GHz; the temporary AP selection was cleared.",
            }
        except Exception as exc:
            diagnostic_event('wifi', 'manual_reconnect_failed', level='error', stage='apply', code=type(exc).__name__)
            rollback = self._roll_back_failed_transaction(journal, str(exc))
            return {
                "success": False,
                "error": "manual_reconnect_failed",
                "message": str(exc),
                "detail": (
                    "The original WiFi profile was restored."
                    if rollback.get("success")
                    else "Automatic recovery requires attention: "
                    + "; ".join(rollback.get("conflicts", []))
                ),
                "rolled_back": rollback.get("rolled_back", False),
                "rollback_conflicts": rollback.get("conflicts", []),
                "connection_recovered": self._profile_is_active(uuid),
                "frequency_before": frequency_before,
                "frequency": self._get_link_frequency(),
                "attempts": 1,
            }

    async def rescan_and_reconnect(self, target_band: str = "high") -> dict:
        """Public bounded manual recovery action for the high-band preference."""
        async with self._get_band_policy_file_gate():
            try:
                file_lock = await self._worker_complete(
                    self._acquire_band_policy_file_lock,
                    cancelled_cleanup=self._release_band_policy_file_lock
                )
            except Exception as exc:
                return {
                    "success": False,
                    "error": "band_policy_lock_failed",
                    "message": "The network-operation lock is unavailable.",
                    "detail": str(exc),
                }
            try:
                async with self._get_network_mutation_lock():
                    if target_band == "high":
                        return await self._worker_complete(self._rescan_and_reconnect_sync)
                    return await self._worker_complete(self._rescan_and_reconnect_sync, target_band)
            except Exception as exc:
                diagnostic_event('wifi', 'manual_reconnect_failed', level='error', stage='admission', code=type(exc).__name__)
                return self._unexpected_response(exc)
            finally:
                self._release_band_policy_file_lock(file_lock)

    @staticmethod
    def _iwd_snapshot_matches(left: dict, right: dict) -> bool:
        return (
            not bool(left.get("ambiguous"))
            and not bool(right.get("ambiguous"))
            and bool(left.get("modifier_present"))
            == bool(right.get("modifier_present"))
            and str(left.get("modifier_value") or "")
            == str(right.get("modifier_value") or "")
        )

    @staticmethod
    def _applied_iwd(applied: dict) -> dict:
        """The iwd modifier recorded in a policy state's "applied" section."""
        return {
            "modifier_present": applied.get("iwd_modifier_present", False),
            "modifier_value": applied.get("iwd_modifier_value", ""),
        }

    def _restart_selected_iwd(self) -> dict:
        """Reload a running, selected manager without starting an inactive one."""
        if self._get_current_backend() != "iwd":
            return {"success": False, "stderr": "The WiFi backend changed; iwd was not restarted."}
        active = self._run_cmd(["/usr/bin/systemctl", "is-active", "iwd"], timeout=3)
        if active.get("stdout", "").strip() != "active":
            return {"success": False, "stderr": "iwd is inactive; Companion did not start it."}
        result = self._run_cmd(["/usr/bin/systemctl", "try-restart", "iwd"], timeout=15)
        if not result.get("success"):
            return result
        if self._get_current_backend() != "iwd":
            return {"success": False, "stderr": "The WiFi backend changed during iwd reload."}
        active = self._run_cmd(["/usr/bin/systemctl", "is-active", "iwd"], timeout=3)
        if active.get("stdout", "").strip() != "active":
            return {"success": False, "stderr": "iwd stopped during reload."}
        return {"success": True}

    def _restore_band_policy_transaction(
        self,
        journal: dict,
        reason: str,
        journal_path: str | None = None,
    ) -> dict:
        """Rollback only mutations whose expected values are still present."""
        path = journal_path or BAND_POLICY_JOURNAL_FILE
        journal["phase"] = "rolling_back"
        journal["rollback_reason"] = reason
        self._save_band_policy_journal(journal, path)
        conflicts = []
        mutations = journal.get("mutations", {})
        backend = self._get_current_backend()
        iwd_only = bool(mutations.get("iwd", {}).get("planned")) and not any(
            mutation.get("planned") for name, mutation in mutations.items()
            if name != "iwd" and isinstance(mutation, dict))
        recovery_active_uuid = self._get_active_connection_uuid()

        iwd_mutation = mutations.get("iwd", {})
        if iwd_mutation.get("planned"):
            current = self._get_iwd_rank_modifier_snapshot()
            expected = {
                "modifier_present": iwd_mutation.get("expected_present", False),
                "modifier_value": iwd_mutation.get("expected_value", ""),
            }
            before = journal.get("iwd_before", {})
            if self._iwd_snapshot_matches(current, expected):
                try:
                    # Persist the required service work before changing the
                    # file. If power is lost after the file restore, recovery
                    # still knows that iwd must be restarted and reconnected.
                    journal["rollback_iwd_restart_required"] = backend != "wpa_supplicant"
                    journal["rollback_iwd_restart_completed"] = False
                    active_uuid = str(journal.get("active_uuid_before") or "")
                    if active_uuid and backend != "wpa_supplicant":
                        journal["rollback_reconnect_required"] = True
                        journal["rollback_reconnect_uuid"] = active_uuid
                        journal["rollback_reconnect_completed"] = False
                    self._save_band_policy_journal(journal, path)
                    self._restore_iwd_rank_modifier(before)
                    restored_iwd = self._get_iwd_rank_modifier_snapshot()
                    if self._iwd_snapshot_matches(restored_iwd, before):
                        journal["rollback_iwd_file_restored"] = True
                        self._save_band_policy_journal(journal, path)
                    else:
                        conflicts.append("iwd restore could not be verified")
                except Exception as e:
                    conflicts.append(f"iwd restore failed: {e}")
            elif not self._iwd_snapshot_matches(current, before):
                conflicts.append("iwd setting changed outside Legion Go 2 Companion")

        nm_mutation = mutations.get("nm_band", {})
        profile = journal.get("profile_before", {})
        uuid = str(profile.get("uuid") or "")
        if nm_mutation.get("planned") and uuid:
            def require_reconnect():
                if profile.get("was_active"):
                    journal["rollback_reconnect_required"] = True
                    journal["rollback_reconnect_uuid"] = uuid
                    journal["rollback_reconnect_completed"] = False
                    self._save_band_policy_journal(journal, path)
            self._restore_nm_field(
                uuid, "802-11-wireless.band", "band", str(nm_mutation.get("expected") or ""),
                str(profile.get("band") or ""), conflicts, before_restore=require_reconnect)

        bssid_mutation = mutations.get("nm_bssid", {})
        if bssid_mutation.get("planned") and uuid:
            self._restore_nm_field(
                uuid, "802-11-wireless.bssid", "BSSID", self._normalize_bssid(bssid_mutation.get("expected")),
                self._normalize_bssid(profile.get("bssid")), conflicts, read=self._normalize_bssid)

        # A native backend switch owns the running manager and connection.
        # Restoring our inactive iwd file must never start iwd alongside WPA,
        # nor reconnect an old profile the user may already have forgotten.
        backend = self._get_current_backend()
        if backend == "wpa_supplicant":
            journal["rollback_iwd_restart_required"] = False
            journal["rollback_iwd_runtime_skipped_backend"] = backend
            if iwd_only or (journal.get("operation") == "automatic_high_band_reconnect" and mutations == {}):
                journal["rollback_reconnect_required"] = False
                journal["rollback_reconnect_skipped_backend"] = backend
            self._save_band_policy_journal(journal, path)
        if journal.get("rollback_iwd_restart_required") and not journal.get(
            "rollback_iwd_restart_completed"
        ):
            restart = self._restart_selected_iwd()
            if not restart.get("success"):
                conflicts.append(f"iwd restart failed: {restart.get('stderr', '')}")
            else:
                journal["rollback_iwd_restart_completed"] = True
                self._save_band_policy_journal(journal, path)

        reconnect_uuid = str(journal.get("rollback_reconnect_uuid") or "")
        automatic = (journal.get("operation") == "automatic_high_band_reconnect"
                     and journal.get("mutations") == {})
        bound_iface = str(journal.get("automatic_iface") or "") if automatic else ""
        if journal.get("rollback_reconnect_required") and backend not in ("iwd", "wpa_supplicant"):
            conflicts.append("The active WiFi backend could not be verified for recovery")
        if (
            journal.get("rollback_reconnect_required")
            and backend in ("iwd", "wpa_supplicant")
            and not journal.get("rollback_reconnect_completed")
            and reconnect_uuid
        ):
            current_uuid = self._get_active_connection_uuid(bound_iface or None)
            changed_connection = (current_uuid not in (None, reconnect_uuid)
                                  or recovery_active_uuid not in (None, reconnect_uuid))
            active = (self._profile_is_active(reconnect_uuid, bound_iface)
                      if bound_iface else self._profile_is_active(reconnect_uuid))
            frequency = ((self._get_link_frequency(bound_iface) if bound_iface else self._get_link_frequency())
                         if active else None)
            device_changed = False
            if automatic and bound_iface and not changed_connection and not active:
                try:
                    with open(f"/sys/class/net/{bound_iface}/ifindex", encoding="ascii") as handle:
                        current_ifindex = int(handle.read(32))
                    device_changed = (self._boot_id() == journal.get("automatic_boot_id")
                                      and current_ifindex != journal.get("automatic_ifindex"))
                    driver = os.path.basename(os.path.realpath(f"/sys/class/net/{bound_iface}/device/driver"))
                    device_changed = device_changed or driver != "mt7921e"
                except (OSError, ValueError):
                    device_changed = True
            if changed_connection or (active and frequency is not None):
                # A connection-only operation has no profile/config writes to
                # undo. Preserve a different network selected by the user.
                journal["rollback_reconnect_completed"] = True
                self._save_band_policy_journal(journal, path)
            elif device_changed:
                conflicts.append("The WiFi device changed during automatic reconnection")
            else:
                if bound_iface:
                    reconnect = self._reconnect_profile(reconnect_uuid, True, cycle=active,
                                                         bound_iface=bound_iface, activation_timeout=5)
                else:
                    reconnect = self._reconnect_profile(
                        reconnect_uuid, True,
                        cycle=not bool(journal.get("rollback_iwd_restart_required")))
                if not reconnect.get("success"):
                    conflicts.append(
                        f"WiFi reconnect failed: {reconnect.get('stderr', '')}"
                    )
                else:
                    journal["rollback_reconnect_completed"] = True
                    self._save_band_policy_journal(journal, path)

        if not conflicts:
            settings_path = str(journal.get("settings_file") or SETTINGS_FILE)
            self._write_band_settings_fields(
                settings_path, journal.get("settings_before", {})
            )
            journal["phase"] = "rolled_back"
        else:
            journal["phase"] = "rollback_failed"
            journal["rollback_conflicts"] = conflicts
        journal["rolled_back_at"] = int(time.time())
        self._save_band_policy_journal(journal, path)
        return {
            "success": not conflicts,
            "rolled_back": not conflicts,
            "conflicts": conflicts,
        }

    def _roll_back_failed_transaction(self, journal: dict, reason: str) -> dict:
        """Undo a failed operation; the journal and watchdog stay unless that succeeded."""
        rollback = self._restore_band_policy_transaction(journal, reason)
        if rollback.get("success"):
            self._cancel_band_policy_rollback(journal)
            self._remove_band_policy_journal()
        return rollback

    @staticmethod
    def _band_policy_rollback_retryable(journal: dict) -> bool:
        mutations = journal.get("mutations")
        profile = journal.get("profile_before")
        return bool(
            journal.get("schema") == 1
            and isinstance(journal.get("settings_before"), dict)
            and journal.get("settings_before")
            and isinstance(mutations, dict)
            and (any(isinstance(mutation, dict) and mutation.get("planned")
                     for mutation in mutations.values())
                 or (journal.get("operation") == "automatic_high_band_reconnect"
                     and journal.get("rollback_reconnect_required") is True
                     and isinstance(profile, dict)
                     and journal.get("rollback_reconnect_uuid") == profile.get("uuid")
                     and journal.get("rollback_reconnect_uuid"))))

    def _recover_band_policy_journal(self) -> dict:
        """Complete/rollback an operation interrupted by a plugin restart."""
        journal = self._load_band_policy_journal()
        if not journal:
            return {"success": True, "recovered": False}
        phase = journal.get("phase")
        if phase in ("pending", "applying", "rolling_back"):
            result = self._restore_band_policy_transaction(
                journal, "interrupted operation"
            )
            if not result.get("success"):
                diagnostic_transition('wifi', 'band_recovery', 'deferred', level='error',
                                      stage='interrupted', count=len(result.get('conflicts', [])), pending=True)
                return {
                    "success": False,
                    "error": "band_policy_recovery_required",
                    "conflicts": result.get("conflicts", []),
                }
        elif phase == "verified":
            self._write_band_settings_fields(
                str(journal.get("settings_file") or SETTINGS_FILE),
                journal.get("settings_after", {}),
            )
            journal["phase"] = "committed"
            self._save_band_policy_journal(journal)
        elif phase == "rollback_failed":
            if not self._band_policy_rollback_retryable(journal):
                diagnostic_transition('wifi', 'band_recovery', 'deferred', level='error',
                                      stage='incomplete', pending=True)
                return {
                    "success": False,
                    "error": "band_policy_recovery_required",
                    "conflicts": journal.get("rollback_conflicts", []),
                }
            result = self._restore_band_policy_transaction(
                journal, "retrying incomplete rollback"
            )
            if not result.get("success"):
                diagnostic_transition('wifi', 'band_recovery', 'deferred', level='error',
                                      stage='retry', count=len(result.get('conflicts', [])), pending=True)
                return {
                    "success": False,
                    "error": "band_policy_recovery_required",
                    "conflicts": result.get("conflicts", []),
                }
        elif phase not in ("committed", "rolled_back"):
            diagnostic_transition('wifi', 'band_recovery', 'deferred', level='error', stage='unknown_phase', pending=True)
            return {
                "success": False,
                "error": "band_policy_recovery_required",
                "conflicts": [f"unknown journal phase: {phase}"],
            }

        self._cancel_band_policy_rollback(journal)
        self._remove_band_policy_journal()
        restored = journal.get('phase') == 'rolled_back'
        diagnostic_transition('wifi', 'band_recovery', 'ready', pending=False,
                              restored=restored, outcome='restored' if restored else 'completed')
        return {"success": True, "recovered": True}

    def _nm_profile_gone(self, uuid: str) -> bool:
        listing = self._run_cmd(["/usr/bin/nmcli", "-t", "-f", "UUID", "con", "show"], timeout=5)
        return bool(uuid) and listing.get("success", False) and uuid not in listing.get("stdout", "").split()

    def _band_policy_ownership_error(self, settings: dict, *, allow_restored: bool = False) -> str:
        mode = settings.get("band_policy", BAND_POLICY_OFF)
        if mode == BAND_POLICY_OFF:
            return ""
        state = settings.get("band_policy_state") or {}
        if state.get("mode") != mode:
            return "Band policy ownership data is incomplete."
        uuid = str(state.get("connection_uuid") or "")
        applied = state.get("applied") or {}
        if state.get("owns_nm_band"):
            current_band, result = self._nmcli_get(uuid, "802-11-wireless.band")
            # An explicit transition may drop a policy whose profile was
            # forgotten or whose band is already back at the recorded original.
            restored = allow_restored and (
                current_band == str((state.get("original") or {}).get("nm_band") or "")
                if result.get("success") else self._nm_profile_gone(uuid))
            if not restored and not result.get("success"):
                return "The managed WiFi profile cannot be read."
            if not restored and current_band != str(applied.get("nm_band") or ""):
                return "The profile band was changed outside Legion Go 2 Companion."
        if state.get("owns_iwd"):
            current_iwd = self._get_iwd_rank_modifier_snapshot()
            if not self._iwd_snapshot_matches(current_iwd, self._applied_iwd(applied)):
                original = (state.get("original") or {}).get("iwd")
                # An explicit user transition may recover an OS rollback to
                # the recorded baseline. Background/status checks remain strict;
                # a different external value is never silently overwritten.
                restored = (allow_restored and isinstance(original, dict)
                            and type(original.get("modifier_present")) is bool
                            and self._iwd_snapshot_matches(current_iwd, original))
                if not restored:
                    return "The iwd band setting was changed outside Legion Go 2 Companion."
        return ""

    def _band_policy_transition_error(self, settings: dict, target: str) -> str:
        """Protect non-owned fields that the requested transition would replace."""
        mode = settings.get("band_policy", BAND_POLICY_OFF)
        state = settings.get("band_policy_state") or {}
        applied = state.get("applied") or {}
        if mode == BAND_POLICY_SIX_ONLY and target in IWD_BAND_POLICIES:
            current_iwd = self._get_iwd_rank_modifier_snapshot()
            if not self._iwd_snapshot_matches(current_iwd, self._applied_iwd(applied)):
                return (
                    "The iwd band setting changed after 6 GHz only was enabled. "
                    "Disable the current policy before replacing it."
                )
        return ""

    async def _verify_band_policy(
        self, mode: str, uuid: str, timeout: int = 24
    ) -> dict:
        deadline = asyncio.get_running_loop().time() + timeout
        last_frequency = None
        while asyncio.get_running_loop().time() < deadline:
            if self._profile_is_active(uuid):
                last_frequency = self._get_link_frequency()
                if mode == BAND_POLICY_SIX_ONLY and last_frequency is not None:
                    if 5925 <= last_frequency < 7125:
                        return {"success": True, "frequency": last_frequency}
                elif mode == BAND_POLICY_STRICT_HIGH and last_frequency is not None:
                    if 4900 <= last_frequency < 7125:
                        return {"success": True, "frequency": last_frequency}
                elif mode == BAND_POLICY_HIGH_ONLY:
                    # This mode is a ranking preference, not a band lock. A
                    # 2.4 GHz link is valid when no usable higher-band BSS is
                    # available; the iwd config itself is verified separately.
                    if last_frequency is not None:
                        return {"success": True, "frequency": last_frequency}
                elif mode == BAND_POLICY_OFF and last_frequency is not None:
                    return {"success": True, "frequency": last_frequency}
            await asyncio.sleep(max(0, min(1, deadline - asyncio.get_running_loop().time())))
        return {
            "success": False,
            "frequency": last_frequency,
            "message": "WiFi did not reconnect on the requested band before timeout.",
        }

    def _get_wifi_interface(self) -> str | None:
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-t", "-f", "DEVICE,TYPE", "dev", "status"]
        )
        if not result["success"]:
            return None
        for line in result["stdout"].split("\n"):
            parts = line.split(":")
            if len(parts) >= 2 and parts[1] == "wifi":
                return parts[0]
        return None

    def _get_active_connection_uuid(
        self, iface: str | None = None, *, require_connected: bool = False
    ) -> str | None:
        if iface is None:
            iface = self._get_wifi_interface()
        if not iface:
            return None
        fields = "GENERAL.STATE,GENERAL.CON-UUID" if require_connected else "GENERAL.CON-UUID"
        result = self._run_cmd(
            ["/usr/bin/nmcli", "-g", fields, "device", "show", iface], timeout=2
        )
        if not result["success"]:
            return None
        value = result.get("stdout", "").strip()
        if require_connected:
            lines = value.splitlines()
            if len(lines) != 2 or not re.fullmatch(r"100(?:\s+\(.*\))?", lines[0]):
                return None
            value = lines[1]
        return value if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value) else None

    def _get_connection_state(self, iface: str | None) -> dict:
        if not iface:
            # Missing inventory also covers a failed/timed-out NM read.
            return {"connection_state": "unknown", "connection_uuid": ""}
        result = self._run_cmd([
            "/usr/bin/nmcli", "-g", "GENERAL.STATE,GENERAL.CON-UUID",
            "device", "show", iface,
        ], timeout=2, preserve_stdout=True)  # keeps the empty CON-UUID line of a disconnected device
        lines = result.get("stdout", "").splitlines()
        match = re.fullmatch(r"(\d+)(?:\s+\(.*\))?", lines[0]) if lines else None
        if not result.get("success") or len(lines) != 2 or not match:
            return {"connection_state": "unknown", "connection_uuid": ""}
        state = int(match.group(1))
        uuid = lines[1] if re.fullmatch(
            r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", lines[1]) else ""
        name = "connected" if state == 100 and uuid else (
            "connecting" if 40 <= state <= 90 else "disconnected")
        return {"connection_state": name, "connection_uuid": uuid}

    def _get_current_backend(self) -> str | None:
        """Return 'iwd', 'wpa_supplicant', or None if unknown.

        Reads NetworkManager's effective configuration (all conf.d layers
        merged in its own precedence), falling back to the known config files
        if that fails, then to the active systemd service. The Go 2 fork never
        writes or switches the backend.
        """
        effective = self._run_cmd(["/usr/bin/NetworkManager", "--print-config"], timeout=3)
        if effective.get("success"):
            section = ""
            for line in effective.get("stdout", "").splitlines():
                line = line.strip()
                if line.startswith("["):
                    section = line
                elif section == "[device]" and line.startswith("wifi.backend="):
                    val = line.partition("=")[2].strip()
                    return val if val in ("iwd", "wpa_supplicant") else None
            paths = ()  # every file is already merged; unset means NM's built-in default
        else:
            paths = (BAZZITE_IWD_CONF, WIFI_BACKEND_CONF, NM_DEFAULT_CONF)
        for path in paths:
            try:
                with open(path, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or line.startswith(";"):
                            continue
                        if line.startswith("wifi.backend"):
                            _, _, val = line.partition("=")
                            val = val.strip()
                            if val in ("iwd", "wpa_supplicant"):
                                return val
            except Exception:
                continue
        # With no authoritative config, competing managers are ambiguous.
        active = [name for name in ("iwd", "wpa_supplicant")
                  if self._run_cmd(["/usr/bin/systemctl", "is-active", name], timeout=3)
                  .get("stdout", "").strip() == "active"]
        return active[0] if len(active) == 1 else None

    # ---- Lifecycle ----

    async def _main(self):
        self._high_band_monitor_ready = False
        try:
            decky.logger.info("WiFi starting")
            band_lock = await self._worker_complete(
                self._acquire_band_policy_file_lock,
                cancelled_cleanup=self._release_band_policy_file_lock
            )
            try:
                band_recovery = await self._worker_complete(self._recover_band_policy_journal)
            finally:
                self._release_band_policy_file_lock(band_lock)
            if not band_recovery.get("success"):
                decky.logger.error(
                    "WiFi is not ready: unresolved network "
                    "recovery state remains on disk"
                )
                return {"success": False, "error": "Unresolved WiFi recovery state remains on disk."}
            info = await self.get_device_info()
            settings = await self._worker_complete(_load_settings)
            settings["driver"] = info.get("driver", "unknown")
            settings["device_family"] = info.get("device_family", "unknown")
            settings["device_label"] = info.get("device_label", "Unknown Device")
            await self._worker_complete(_save_settings, settings)

            decky.logger.info(
                f"WiFi ready: device={info.get('device_label')}, "
                f"family={info.get('device_family')}, driver={info.get('driver')}"
            )
            self._high_band_monitor_ready = True
            self._start_high_band_monitor(settings)
            return {"success": True}
        except Exception as e:
            diagnostic_event('wifi', 'main_failed', level='error', error=e, repeat=60)
            return {"success": False, "error": str(e)}

    async def _unload(self):
        self._high_band_monitor_ready = False
        await self._stop_high_band_monitor()
        decky.logger.info("WiFi unloading")

    async def _release_all(self) -> dict:
        """Undo everything this module owns (the band policy); returns the result."""
        return {"band_policy": await self.set_band_policy(BAND_POLICY_OFF)}

    async def _uninstall(self):
        try:
            decky.logger.info("WiFi uninstalling")
            results = await self._release_all()
            if not results["band_policy"].get("success"):
                diagnostic_event('wifi', 'uninstall_restore_unconfirmed', level='error', stage='band_policy', pending=True)
            else:
                for path in (SETTINGS_FILE, SETTINGS_FILE + ".bak",
                             BAND_POLICY_JOURNAL_FILE):
                    try:
                        os.remove(path)
                    except FileNotFoundError:
                        pass
        except Exception as e:
            diagnostic_event('wifi', 'uninstall_failed', level='error', error=e, repeat=60)

    # ---- Hardware detection ----

    def _detect_device_family(self) -> tuple[str, str, str]:
        """Read DMI product_name and return (raw_product, family_id, display_label)."""
        try:
            with open("/sys/devices/virtual/dmi/id/product_name", "r") as f:
                product = f.read().strip()
        except Exception:
            return ("unknown", "unknown", "Unknown Device")

        if product in LEGION_GO_2_PRODUCTS:
            return (product, "legion_go_2", "Legion Go 2")
        return (product, "unknown", "Unknown Device")

    def _detect_wifi_driver(self, iface: str | None = None) -> str:
        """Kernel driver (module name) of the active WiFi interface via sysfs."""
        if iface is None:
            iface = self._get_wifi_interface()
        if not iface:
            return "unknown"
        try:
            return os.path.basename(os.path.realpath(f"/sys/class/net/{iface}/device/driver/module"))
        except Exception:
            return "unknown"

    async def get_device_info(self) -> dict:
        return await self._worker_complete(self._get_device_info_sync)

    def _get_device_info_sync(self) -> dict:
        try:
            _, device_family, device_label = self._detect_device_family()
            return {
                "success": True,
                "driver": self._detect_wifi_driver(),
                "device_family": device_family,
                "device_label": device_label,
            }
        except Exception as e:
            diagnostic_event('wifi', 'get_device_info_sync_failed', level='error', error=e, repeat=60)
            return {
                "success": True,
                "driver": "unknown",
                "device_family": "unknown",
                "device_label": "Unknown Device",
            }

    def _unexpected_response(self, e: Exception) -> dict:
        """Standard error dict for the catch-all exception handler in every
        setter. Callers log the error separately with the setter name."""
        return {"success": False, "error": "unexpected", "message": str(e)}

    def _nmcli_modify(self, uuid: str, key: str, value: str, timeout: int = 5) -> dict:
        """Run `nmcli con mod uuid <uuid> <key> <value>`. Returns the
        _run_cmd dict so callers can handle success/failure themselves."""
        return self._run_cmd(
            ["/usr/bin/nmcli", "con", "mod", "uuid", uuid, key, value],
            timeout=timeout,
        )

    # ---- Status ----

    async def get_status(self) -> dict:
        # NetworkManager/iw are blocking system tools. Keep them away from
        # Decky's shared asyncio loop so a slow status read cannot stall other
        # Companion modules.
        return await asyncio.to_thread(self._get_status_sync)

    def _get_status_sync(self) -> dict:
        # Short read-only timeouts plus a 10-second UI interval keep background
        # work bounded while the panel is open.
        T = 2

        try:
            settings = _load_settings()
            iface = self._get_wifi_interface()
            connection = self._get_connection_state(iface)
            connected = connection["connection_state"] == "connected"

            status = {
                "success": True,
                "connected": connected,
                "settings": settings,
                "live": connection,
                "drift": {},
            }

            recovery_issues = self._get_network_recovery_issues()
            if recovery_issues:
                status["live"]["recovery_required"] = True
                status["live"]["recovery_errors"] = recovery_issues
                status["drift"]["recovery"] = True

            # Backend info is read-only and system-wide.
            backend = self._get_current_backend() or ""
            saved_policy = settings.get("band_policy", BAND_POLICY_OFF)
            inactive = ((saved_policy in IWD_BAND_POLICIES and backend != "iwd")
                        or (saved_policy == BAND_POLICY_SIX_ONLY and backend != "wpa_supplicant"))
            status["live"].update(wifi_backend=backend, saved_policy_inactive=inactive,
                                   band_policy=BAND_POLICY_OFF if inactive else saved_policy)
            status["live"]["six_ghz_only_supported"] = (
                backend == "wpa_supplicant" and self._nm_supports_six_ghz_only())
            status["live"]["high_band_discovery_pending"] = bool(
                backend == "iwd" and getattr(self, "_high_band_discovery_pending", False))
            journal = self._load_band_policy_journal()
            status["live"]["band_policy_restore_available"] = bool(
                backend in ("iwd", "wpa_supplicant")
                and settings.get("device_family") == "legion_go_2"
                and settings.get("driver") == "mt7921e"
                and (saved_policy != BAND_POLICY_OFF or journal)
                and (not journal or (journal.get("phase") == "rollback_failed"
                                     and self._band_policy_rollback_retryable(journal)))
                and not self._band_policy_ownership_error(settings, allow_restored=True))

            # A strict policy can leave WiFi disconnected. Ownership and drift
            # must still be visible so the user can safely turn the policy off.
            ownership_error = self._band_policy_ownership_error(settings)
            if ownership_error:
                status["drift"]["band_policy"] = True
                status["live"]["band_policy_error"] = ownership_error

            if not connected:
                return status

            # Link info
            link_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "link"], timeout=T
            )
            link_out = link_result.get("stdout", "")
            ssid = re.search(r"(?m)^[ \t]*SSID: ?(.*)$", link_out)
            if ssid:
                decoded_ssid = self._iw_ssid(ssid.group(1))
                if decoded_ssid is not None:
                    status["live"]["ssid"] = decoded_ssid
            for line in link_out.split("\n"):
                line = line.strip()
                if line.startswith("signal:"):
                    status["live"]["signal_dbm"] = line.split(":", 1)[1].strip()
                elif "tx bitrate:" in line:
                    status["live"]["tx_bitrate"] = line.split("tx bitrate:", 1)[
                        1
                    ].strip()
                elif line.startswith("freq:"):
                    status["live"]["frequency"] = line.split(":", 1)[1].strip()

            # NM activation alone can outlive the radio link during a switch.
            # Verify the same activated profile after reading the kernel link.
            if (not link_result.get("success") or not status["live"].get("frequency")
                    or self._get_active_connection_uuid(iface, require_connected=True)
                    != connection["connection_uuid"]):
                status["connected"] = False
                status["live"]["connection_state"] = "unknown"
                for field in ("ssid", "frequency", "signal_dbm", "tx_bitrate"):
                    status["live"].pop(field, None)
                return status

            if backend == "iwd" and saved_policy == BAND_POLICY_STRICT_HIGH:
                try:
                    linked_frequency = float(status["live"].get("frequency", ""))
                except (TypeError, ValueError):
                    linked_frequency = None
                if (linked_frequency is not None
                        and 2400 <= linked_frequency < 2500):
                    status["drift"]["band_policy"] = True
                    status["live"].setdefault("band_policy_error", (
                        "Only 5/6 GHz is selected, but the live WiFi link uses 2.4 GHz."
                    ))

            # Channel info - parse to "36 (80 MHz)" format
            info_result = self._run_cmd(
                ["/usr/bin/iw", "dev", iface, "info"], timeout=T
            )
            for line in info_result.get("stdout", "").split("\n"):
                line = line.strip()
                if line.startswith("channel"):
                    # Raw: "channel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz"
                    parts = line.split(",")
                    chan_num = ""
                    width = ""
                    if parts:
                        tokens = parts[0].split()
                        if len(tokens) >= 2:
                            chan_num = tokens[1]
                    for part in parts:
                        part = part.strip()
                        if part.startswith("width:"):
                            width = part.split(":", 1)[1].strip()
                    if chan_num and width:
                        status["live"]["channel"] = f"{chan_num} ({width})"
                    elif chan_num:
                        status["live"]["channel"] = chan_num
                    else:
                        status["live"]["channel"] = line

            return status
        except Exception as e:
            diagnostic_event('wifi', 'status_failed', level='warning', repeat=60, code=type(e).__name__)
            return self._unexpected_response(e)

    # ---- Optimization setters ----

    async def set_band_policy(
        self, mode: str, allow_unverified_scan: bool = False
    ) -> dict:
        """Run one band transaction while fenced from the rollback service."""
        # Interrupt discovery before waiting for the gate held by its worker.
        # Monitor tasks live on Decky's loop, not the transaction worker loop.
        await self._stop_high_band_monitor()
        async with self._get_band_policy_file_gate():
            try:
                file_lock = await self._worker_complete(
                    self._acquire_band_policy_file_lock,
                    cancelled_cleanup=self._release_band_policy_file_lock
                )
            except Exception as e:
                diagnostic_event('wifi', 'band_policy_failed', level='error', stage='lock', code=type(e).__name__)
                await self._restart_high_band_monitor()
                return {
                    "success": False,
                    "error": "band_policy_lock_failed",
                    "message": "The cross-process rollback lock is unavailable.",
                    "detail": str(e),
                    "rolled_back": False,
                }
            try:
                async with self._get_network_mutation_lock():
                    return await self._worker_complete(
                        lambda: asyncio.run(
                            self._set_band_policy_impl(
                                mode,
                                allow_unverified_scan=allow_unverified_scan,
                            )
                        )
                    )
            finally:
                # LOCK_UN and close are local, non-blocking operations. Doing
                # this directly also guarantees progress if executor threads
                # are busy with unrelated work.
                self._release_band_policy_file_lock(file_lock)
                await self._restart_high_band_monitor()

    async def _set_band_policy_impl(
        self, mode: str, allow_unverified_scan: bool = False,
    ) -> dict:
        """Atomically select one Go 2 band policy, or restore the prior state.

        Every mutating operation is protected by both a process-local lock and
        a systemd rollback timer.  The timer reads the on-disk journal and
        restores only values that still equal the values written by us.
        """
        if not isinstance(mode, str) or mode not in BAND_POLICIES:
            return {
                "success": False,
                "error": "invalid_band_policy",
                "message": (
                    "Band policy must be off, six_ghz_only, "
                    "five_six_no_24, or five_six_only."
                ),
                "band_policy": BAND_POLICY_OFF,
                "rolled_back": False,
            }
        if type(allow_unverified_scan) is not bool or (
            allow_unverified_scan and mode != BAND_POLICY_STRICT_HIGH
        ):
            return {
                "success": False,
                "error": "invalid_scan_override",
                "message": "Unverified scan attempts apply only to Only 5/6 GHz.",
                "band_policy": BAND_POLICY_OFF,
                "rolled_back": False,
            }

        backend = self._get_current_backend()
        if backend not in ("iwd", "wpa_supplicant"):
            return {"success": False, "error": "unsupported_backend",
                    "message": "The active WiFi backend could not be verified; no changes were made."}
        existing_journal = self._load_band_policy_journal()
        if existing_journal:
            recovery = self._recover_band_policy_journal()
            if not recovery.get("success") or self._load_band_policy_journal():
                remaining = self._load_band_policy_journal() or existing_journal
                return {
                    "success": False,
                    "error": "band_policy_recovery_required",
                    "message": (
                        "A previous band-policy rollback needs manual "
                        "recovery; no new network changes were made."
                    ),
                    "band_policy": _load_settings().get(
                        "band_policy", BAND_POLICY_OFF
                    ),
                    "rolled_back": False,
                    "rollback_conflicts": remaining.get(
                        "rollback_conflicts", recovery.get("conflicts", [])
                    ),
                    "recovery_state_preserved": True,
                }
        backend = self._get_current_backend()
        if backend not in ("iwd", "wpa_supplicant"):
            return {"success": False, "error": "unsupported_backend",
                    "message": "The WiFi backend changed during recovery; retry after it is ready."}
        settings = _load_settings()
        current_mode = settings.get("band_policy", BAND_POLICY_OFF)
        if mode in IWD_BAND_POLICIES and backend != "iwd":
            return {"success": False, "error": "unsupported_backend",
                    "message": "High-band preference requires the iwd WiFi backend.",
                    "band_policy": current_mode, "rolled_back": False}
        if mode == BAND_POLICY_SIX_ONLY and backend != "wpa_supplicant":
            return {"success": False, "error": "preflight_failed",
                    "message": "6 GHz only requires the native WPA WiFi backend.",
                    "band_policy": current_mode, "rolled_back": False}

        if current_mode not in BAND_POLICIES:
            return {
                "success": False,
                "error": "invalid_saved_band_policy",
                "message": "Saved band policy is not recognized; no network changes were made.",
                "band_policy": current_mode,
                "rolled_back": False,
            }

        preference_needs_upgrade = (
            current_mode == BAND_POLICY_HIGH_ONLY
            and mode == BAND_POLICY_HIGH_ONLY
            and str(
                ((settings.get("band_policy_state") or {}).get("applied") or {}).get(
                    "iwd_modifier_value"
                )
                or ""
            )
            != BAND_PREFERENCE_2_4_MODIFIER
        )
        ownership_error = self._band_policy_ownership_error(settings)
        transition_ownership_error = self._band_policy_ownership_error(
            settings, allow_restored=True)
        preference_needs_reapply = bool(ownership_error and not transition_ownership_error)
        if current_mode == mode and not preference_needs_upgrade and not preference_needs_reapply:
            ownership_error = self._band_policy_ownership_error(settings)
            if ownership_error:
                return {
                    "success": False,
                    "error": "ownership_conflict",
                    "message": ownership_error,
                    "band_policy": current_mode,
                    "rolled_back": False,
                }
            return {
                "success": True,
                "band_policy": current_mode,
                "reconnected": False,
                "rolled_back": False,
                "message": "Band policy is already active.",
            }

        ownership_error = transition_ownership_error
        if ownership_error:
            return {
                "success": False,
                "error": "ownership_conflict",
                "message": ownership_error,
                "band_policy": current_mode,
                "rolled_back": False,
            }

        transition_error = self._band_policy_transition_error(settings, mode)
        if transition_error:
            return {
                "success": False,
                "error": "transition_conflict",
                "message": transition_error,
                "band_policy": current_mode,
                "rolled_back": False,
            }

        if mode == BAND_POLICY_SIX_ONLY:
            if not await asyncio.to_thread(self._nm_supports_six_ghz_only):
                return {
                    "success": False,
                    "error": "preflight_failed",
                    "message": "NetworkManager 1.58 or newer is required for 6 GHz only.",
                    "band_policy": current_mode,
                    "rolled_back": False,
                }
            capabilities = await asyncio.to_thread(
                self._get_band_policy_capabilities_sync, True, True
            )
            if not capabilities.get("six_ghz_only_available"):
                return {
                    "success": False,
                    "error": "preflight_failed",
                    "message": capabilities.get("reason_six_ghz_only")
                    or "Band policy preflight failed.",
                    "band_policy": current_mode,
                    "rolled_back": False,
                    "capabilities": capabilities,
                }
        elif mode in IWD_BAND_POLICIES:
            _, device_family, _ = self._detect_device_family()
            driver = self._detect_wifi_driver()
            if device_family != "legion_go_2" or driver != "mt7921e":
                return {
                    "success": False,
                    "error": "unsupported_device",
                    "message": (
                        "High-band preference is enabled only on a "
                        "Legion Go 2 with the MediaTek MT7922 driver."
                    ),
                    "band_policy": current_mode,
                    "rolled_back": False,
                }
            if self._get_current_backend() != "iwd":
                return {
                    "success": False,
                    "error": "unsupported_backend",
                    "message": "High-band preference requires the iwd WiFi backend.",
                    "band_policy": current_mode,
                    "rolled_back": False,
                }
            iwd_service = self._run_cmd(
                ["/usr/bin/systemctl", "is-active", "iwd"], timeout=3
            )
            if iwd_service.get("stdout", "").strip() != "active":
                return {
                    "success": False,
                    "error": "iwd_inactive",
                    "message": "The iwd service is not active.",
                    "band_policy": current_mode,
                    "rolled_back": False,
                }
            if mode == BAND_POLICY_STRICT_HIGH:
                # DFS/6 GHz BSS entries can be omitted from a scan even
                # when iwd can connect. A confirmed user attempt may skip
                # that unreliable observation, but never the hardware,
                # backend, ownership, watchdog, or live-link checks.
                capabilities = await asyncio.to_thread(
                    self._get_band_policy_capabilities_sync,
                    not allow_unverified_scan,
                )
                available_key = (
                    "five_six_only_try_available" if allow_unverified_scan
                    else "five_six_only_available"
                )
                reason_key = (
                    "reason_five_six_only_try" if allow_unverified_scan
                    else "reason_five_six_only"
                )
                if not capabilities.get(available_key):
                    return {
                        "success": False,
                        "error": "preflight_failed",
                        "message": capabilities.get(reason_key)
                        or "No usable 5/6 GHz access point was verified.",
                        "band_policy": current_mode,
                        "rolled_back": False,
                        "capabilities": capabilities,
                    }

        active_uuid = self._get_active_connection_uuid()
        state = settings.get("band_policy_state") or {}
        inactive_iwd_cleanup = (mode == BAND_POLICY_OFF
                                and current_mode in IWD_BAND_POLICIES
                                and backend == "wpa_supplicant")
        profileless_iwd_transition = (
            (not active_uuid or inactive_iwd_cleanup)
            and mode in (BAND_POLICY_OFF, *IWD_BAND_POLICIES)
            and current_mode in (BAND_POLICY_OFF, *IWD_BAND_POLICIES)
        )
        if mode != BAND_POLICY_OFF:
            profile_uuid = active_uuid
            if current_mode == BAND_POLICY_SIX_ONLY:
                managed_uuid = str(state.get("connection_uuid") or "")
                if managed_uuid and managed_uuid != active_uuid:
                    return {
                        "success": False,
                        "error": "managed_profile_not_active",
                        "message": (
                            "Disable 6 GHz only on its managed profile before "
                            "activating another policy."
                        ),
                        "band_policy": current_mode,
                        "rolled_back": False,
                    }
        elif current_mode == BAND_POLICY_SIX_ONLY:
            profile_uuid = str(state.get("connection_uuid") or "")
        elif current_mode in IWD_BAND_POLICIES:
            # The 5/6 GHz policy owns only the global iwd Rank key.  Its
            # original profile may have been forgotten since activation;
            # removing that global key must not depend on the old UUID.
            profile_uuid = "" if inactive_iwd_cleanup else str(active_uuid or "")
        else:
            profile_uuid = str(active_uuid or state.get("connection_uuid") or "")

        if not profile_uuid and not profileless_iwd_transition:
            if mode == BAND_POLICY_OFF and current_mode == BAND_POLICY_OFF:
                return {
                    "success": True,
                    "band_policy": BAND_POLICY_OFF,
                    "reconnected": False,
                    "rolled_back": False,
                }
            return {
                "success": False,
                "error": "no_wifi",
                "message": "No managed WiFi profile was found.",
                "band_policy": current_mode,
                "rolled_back": False,
            }

        if profileless_iwd_transition:
            # No NetworkManager field is owned or changed in this path.
            band_before = ""
        else:
            band_before, band_result = self._nmcli_get(
                profile_uuid, "802-11-wireless.band"
            )
            if (not band_result.get("success") and mode == BAND_POLICY_OFF
                    and current_mode == BAND_POLICY_SIX_ONLY
                    and self._nm_profile_gone(profile_uuid)):
                # Forgotten profile: nothing left to restore, just drop ownership.
                band_before = str((state.get("original") or {}).get("nm_band") or "")
                band_result = {"success": True}
            if not band_result.get("success"):
                return {
                    "success": False,
                    "error": "profile_read_failed",
                    "message": "The WiFi profile could not be read.",
                    "detail": band_result.get("stderr", ""),
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

        iwd_before = self._get_iwd_rank_modifier_snapshot()
        if mode in IWD_BAND_POLICIES:
            modifier_error = (self._iwd_band_modifier_error(iwd_before, "five")
                              or self._iwd_band_modifier_error(iwd_before, "six"))
            if modifier_error:
                return {
                    "success": False,
                    "error": "preflight_failed",
                    "message": modifier_error,
                    "band_policy": current_mode,
                    "rolled_back": False,
                }
        profile_was_active = bool(profile_uuid and profile_uuid == active_uuid)

        # First calculate the neutral/original values after removing the
        # currently owned policy.  This is what makes transitions mutually
        # exclusive rather than stacking two independent toggles.
        neutral_band = band_before
        neutral_iwd = iwd_before
        if current_mode == BAND_POLICY_SIX_ONLY:
            neutral_band = str((state.get("original") or {}).get("nm_band") or "")
        elif current_mode in IWD_BAND_POLICIES:
            neutral_iwd = (state.get("original") or {}).get("iwd") or {}

        final_band = neutral_band
        final_iwd = neutral_iwd
        if mode == BAND_POLICY_SIX_ONLY:
            final_band = "6GHz"
        elif mode in IWD_BAND_POLICIES:
            final_iwd = {
                "file_exists": True,
                "rank_section_present": True,
                "modifier_present": True,
                "modifier_value": (
                    BAND_STRICT_2_4_MODIFIER if mode == BAND_POLICY_STRICT_HIGH
                    else BAND_PREFERENCE_2_4_MODIFIER
                ),
            }

        if mode in (BAND_POLICY_SIX_ONLY, *IWD_BAND_POLICIES) and neutral_band:
            return {
                "success": False,
                "error": "external_band_configuration",
                "message": (
                    "The active profile has a band setting not owned by this "
                    "plugin. Clear it before enabling the high-band preference."
                ),
                "band_policy": current_mode,
                "rolled_back": False,
            }

        # OFF only removes the plugin's own modifier and must never be blocked
        # (uninstall/withdrawal); the reconnect below re-checks the profiles
        # and a refused reconnect leaves NM autoconnect, like the profileless path.
        if (mode != BAND_POLICY_OFF and backend == "iwd" and active_uuid
                and not self._iwd_snapshot_matches(iwd_before, final_iwd)):
            profile_conflict = self._get_iwd_profile_conflict(active_uuid, allow_disabled_duplicates=True)
            if profile_conflict:
                return {
                    "success": False,
                    "error": "ambiguous_iwd_profiles",
                    "message": profile_conflict,
                    "band_policy": current_mode,
                    "rolled_back": False,
                }

        settings_before = self._band_settings_fields(settings)
        operation_id = f"{os.getpid()}-{int(time.time() * 1000)}"
        journal = {
            "schema": 1,
            "operation_id": operation_id,
            "phase": "pending",
            "created_at": int(time.time()),
            "settings_file": SETTINGS_FILE,
            "settings_before": settings_before,
            "mode_before": current_mode,
            "mode_target": mode,
            "profile_before": {
                "uuid": profile_uuid,
                "band": band_before,
                "was_active": profile_was_active,
            },
            "active_uuid_before": str(active_uuid or ""),
            "backend_before": backend,
            "iwd_before": iwd_before,
            "mutations": {},
        }
        self._save_band_policy_journal(journal)
        watchdog = self._schedule_band_policy_rollback(journal)
        if not watchdog.get("success"):
            self._remove_band_policy_journal()
            return {
                "success": False,
                "error": "rollback_watchdog_failed",
                "message": "Automatic rollback could not be armed; no changes were made.",
                "detail": watchdog.get("stderr", ""),
                "band_policy": current_mode,
                "rolled_back": False,
            }

        try:
            journal["phase"] = "applying"
            self._save_band_policy_journal(journal)
            iwd_changed = False
            nm_changed = False

            def apply_nm_band(value: str):
                nonlocal band_before, nm_changed
                if band_before == value:
                    return
                journal["mutations"]["nm_band"] = {
                    "planned": True,
                    "expected": value,
                    "applied": False,
                }
                self._save_band_policy_journal(journal)
                result = self._nmcli_modify(
                    profile_uuid, "802-11-wireless.band", value, timeout=8
                )
                if not result.get("success"):
                    raise RuntimeError(
                        result.get("stderr") or "NetworkManager rejected the band setting"
                    )
                observed_band, observed_result = self._nmcli_get(
                    profile_uuid, "802-11-wireless.band"
                )
                if not observed_result.get("success") or observed_band != value:
                    raise RuntimeError(
                        "NetworkManager band verification did not match the request"
                    )
                journal["mutations"]["nm_band"]["applied"] = True
                self._save_band_policy_journal(journal)
                band_before = value
                nm_changed = True

            def apply_iwd(snapshot: dict):
                nonlocal iwd_before, iwd_changed
                if self._iwd_snapshot_matches(iwd_before, snapshot):
                    return
                expected_present = bool(snapshot.get("modifier_present"))
                expected_value = str(snapshot.get("modifier_value") or "")
                journal["mutations"]["iwd"] = {
                    "planned": True,
                    "expected_present": expected_present,
                    "expected_value": expected_value,
                    "applied": False,
                }
                self._save_band_policy_journal(journal)
                if expected_present:
                    self._set_iwd_rank_modifier(expected_value)
                else:
                    self._restore_iwd_rank_modifier(snapshot)
                journal["mutations"]["iwd"]["applied"] = True
                self._save_band_policy_journal(journal)
                iwd_before = self._get_iwd_rank_modifier_snapshot()
                if not self._iwd_snapshot_matches(iwd_before, snapshot):
                    raise RuntimeError(
                        "iwd band-policy verification did not match the request"
                    )
                iwd_changed = True

            # An iwd-to-iwd transition must make one journaled file write.
            # Restoring the baseline first would overwrite the journal's
            # expected state and make interruption recovery ambiguous.
            if current_mode in IWD_BAND_POLICIES and mode not in IWD_BAND_POLICIES:
                apply_iwd(neutral_iwd)
            if current_mode == BAND_POLICY_SIX_ONLY:
                apply_nm_band(neutral_band)

            if mode == BAND_POLICY_SIX_ONLY:
                apply_nm_band(final_band)
            elif mode in IWD_BAND_POLICIES:
                apply_iwd(final_iwd)

            if self._get_current_backend() != backend:
                raise RuntimeError("The WiFi backend changed during the operation.")
            iwd_runtime_changed = iwd_changed and backend == "iwd"
            if iwd_runtime_changed:
                restart = self._restart_selected_iwd()
                if not restart.get("success"):
                    raise RuntimeError(
                        restart.get("stderr") or "iwd restart failed"
                    )

            reconnect_uuid = ""
            if self._get_current_backend() != backend:
                raise RuntimeError("The WiFi backend changed before reconnection.")
            if iwd_runtime_changed and active_uuid:
                reconnect_uuid = active_uuid
            elif nm_changed and profile_was_active:
                reconnect_uuid = profile_uuid
            reconnected = bool(reconnect_uuid)
            # Turning a policy OFF is never rolled back over a failed
            # reconnect: that would put the restriction back.
            not_reconnected = ""
            if reconnect_uuid:
                current_uuid = self._get_active_connection_uuid()
                if current_uuid not in (None, reconnect_uuid):
                    raise RuntimeError("The active WiFi profile changed; the new connection was preserved.")
                reconnect = self._reconnect_profile(
                    reconnect_uuid, True, cycle=not iwd_runtime_changed
                )
                if not reconnect.get("success"):
                    if mode != BAND_POLICY_OFF:
                        raise RuntimeError(
                            reconnect.get("stderr") or "WiFi reconnect failed"
                        )
                    not_reconnected = reconnect.get("stderr") or "WiFi reconnect failed"

            if mode == BAND_POLICY_HIGH_ONLY and not profile_uuid:
                verification = {
                    "success": True,
                    "frequency": self._get_link_frequency(),
                }
            elif mode != BAND_POLICY_OFF:
                verification = await self._verify_band_policy(
                    mode, profile_uuid
                )
                if not verification.get("success"):
                    raise RuntimeError(
                        verification.get("message") or "Band verification failed"
                    )
            elif reconnect_uuid and not not_reconnected:
                verification = await self._verify_band_policy(
                    BAND_POLICY_OFF, reconnect_uuid
                )
                if not verification.get("success"):
                    not_reconnected = verification.get("message") or "WiFi reconnect failed"
                    verification = {"success": True, "frequency": self._get_link_frequency()}
            else:
                verification = {"success": True, "frequency": self._get_link_frequency()}

            if self._get_current_backend() != backend:
                raise RuntimeError("The WiFi backend changed before verification completed.")

            if mode == BAND_POLICY_OFF:
                next_state = {}
            else:
                next_state = {
                    "schema": 1,
                    "mode": mode,
                    "connection_uuid": profile_uuid,
                    "original": {
                        "nm_band": neutral_band,
                        "iwd": neutral_iwd,
                    },
                    "owns_nm_band": mode == BAND_POLICY_SIX_ONLY,
                    "owns_iwd": mode in IWD_BAND_POLICIES,
                    "applied": {
                        "nm_band": final_band,
                        "iwd_modifier_present": bool(
                            final_iwd.get("modifier_present")
                        ),
                        "iwd_modifier_value": str(
                            final_iwd.get("modifier_value") or ""
                        ),
                    },
                    "activated_at": int(time.time()),
                }
            settings_after = {
                "band_policy": mode,
                "band_policy_state": next_state,
                "band_preference_enabled": mode == BAND_POLICY_HIGH_ONLY,
            }
            journal["settings_after"] = settings_after
            journal["phase"] = "verified"
            journal["verified_frequency"] = verification.get("frequency")
            self._save_band_policy_journal(journal)
            self._write_band_settings_fields(SETTINGS_FILE, settings_after)
            journal["phase"] = "committed"
            self._save_band_policy_journal(journal)
            self._cancel_band_policy_rollback(journal)
            self._remove_band_policy_journal()
            return {
                "success": True,
                "band_policy": mode,
                "reconnected": reconnected and not not_reconnected,
                "rolled_back": False,
                "frequency": verification.get("frequency"),
                "message": ("Saved iwd preference cleared; the WPA connection was not changed."
                            if inactive_iwd_cleanup else
                            "Band policy cleared; WiFi has not reconnected yet."
                            if not_reconnected else "Band policy applied and verified."),
            }
        except Exception as e:
            diagnostic_event('wifi', 'band_policy_failed', level='error', stage='apply', code=type(e).__name__)
            rollback = self._roll_back_failed_transaction(journal, str(e))
            return {
                "success": False,
                "error": "band_policy_apply_failed",
                "message": str(e),
                "band_policy": current_mode,
                "rolled_back": rollback.get("rolled_back", False),
                "rollback_conflicts": rollback.get("conflicts", []),
            }

    async def set_band_preference(self, enabled: bool, band: str = "a") -> dict:
        """Prefer 5/6 GHz while retaining 2.4 GHz as a fallback."""
        if type(enabled) is not bool:
            return {
                "success": False,
                "error": "invalid_band_preference_state",
                "message": "Band preference must be a boolean.",
            }
        return await self.set_band_policy(
            BAND_POLICY_HIGH_ONLY if enabled else BAND_POLICY_OFF
        )



def _run_band_policy_rollback_cli(journal_path: str) -> int:
    diagnostic_event('wifi', 'watchdog_recovery_started', operation='rollback')
    try:
        result = _run_band_policy_rollback_cli_impl(journal_path)
    except Exception as exc:
        diagnostic_event('wifi', 'watchdog_recovery_failed', level='error',
                         code=type(exc).__name__, errno=getattr(exc, 'errno', None), pending=True)
        raise
    diagnostic_event('wifi', 'watchdog_recovery_finished', level='info' if result == 0 else 'error',
                     returncode=result, pending=result != 0, outcome='complete' if result == 0 else 'deferred')
    return result


def _run_band_policy_rollback_cli_impl(journal_path: str) -> int:
    """Entry point used only by the root-owned transient systemd watchdog."""
    path = os.path.realpath(os.path.abspath(journal_path))
    if os.path.basename(path) != "band-policy-transaction.json":
        return 2
    try:
        journal_stat = os.stat(path)
        parent_stat = os.stat(os.path.dirname(path))
        if os.name == "posix":
            if journal_stat.st_mode & 0o022 or parent_stat.st_mode & 0o022:
                return 2
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                if journal_stat.st_uid != 0 or parent_stat.st_uid != 0:
                    return 2
    except FileNotFoundError:
        # A committed worker can remove the journal just before its timer
        # starts. There is nothing left to restore in that race.
        return 0
    except OSError:
        return 2
    plugin = Plugin()
    try:
        file_lock = plugin._acquire_band_policy_file_lock()
    except Exception:
        return 1
    try:
        journal = plugin._load_band_policy_journal(path)
        # The worker may have committed and removed the journal while this
        # timer service was waiting for the lock.
        if not journal:
            return 0
        if journal.get("schema") != 1:
            return 2
        settings_path = os.path.realpath(
            os.path.abspath(str(journal.get("settings_file") or ""))
        )
        if os.path.basename(settings_path) != os.path.basename(SETTINGS_FILE):
            return 2
        # A verified/committed transaction must remain committed even if
        # stopping the timer raced with its activation.
        if journal.get("phase") == "rollback_failed":
            if not plugin._band_policy_rollback_retryable(journal):
                return 1
        elif journal.get("phase") not in ("pending", "applying", "rolling_back"):
            return 0
        result = plugin._restore_band_policy_transaction(
            journal, "automatic watchdog timeout", journal_path=path
        )
        return 0 if result.get("success") else 1
    finally:
        plugin._release_band_policy_file_lock(file_lock)


if __name__ == "__main__":
    configure_worker('wifi')
    if len(sys.argv) == 3 and sys.argv[1] == "--rollback-band-policy":
        raise SystemExit(_run_band_policy_rollback_cli(sys.argv[2]))
    raise SystemExit(2)
