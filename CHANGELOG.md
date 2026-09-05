# 0.6.1 — reliability audit

- Preserve evidence when both settings copies are damaged and pause affected controls instead of treating lost preferences or ownership as a first installation. Bound JSON size and nesting, recover valid backups, and keep memory consistent with a file already replaced if directory synchronization fails.
- Retain both possible owned button mappings during an interrupted remapper change, so disabling can restore the original mapping without replacing unrelated edits.
- Finish transactions through repeated cancellation, drain background hardware writes when a conflicting plugin is detected, and isolate module recovery failures during startup.
- Prevent overlapping status polls and stale replies from reverting newer UI choices. Stop delayed game reports across module stop/start and keep native dropdown selections working while the overlay hides.
- Show each main-menu summary as soon as its read completes and retain confirmed summaries when reopening the panel. Compatibility checks with unchanged module state no longer discard pending reads; disabled modules and old plugin instances cannot restore stale data.
- Retry missing OLED panel/backlight discovery without rescanning a healthy display, and close notification descriptors when setup fails.
- Check installed-plugin conflicts from the installation directory even when Companion is linked to another checkout, and reject non-boolean Wi-Fi preference requests.
- Report hard RGB, battery and controller startup failures to module management. Let battery, controller and remapping controls recover after a valid settings file is restored without requiring a Decky restart.
- Retry pending module withdrawal during uninstall, preserve failed recovery records and finish cleanup through repeated cancellation.
- Remove unused standalone-plugin update RPCs and download paths. Keep the pinned RyzenAdj download and both archive and binary SHA256 checks.
- Add GitHub Actions for backend tests on Linux and Windows, frontend checks and package validation, with pinned actions and no release publishing.
- Retry transient battery and controller discovery failures promptly after startup or wake, with increasing retry intervals for persistent failures and normal low-frequency checks after recovery.

# 0.6.0 — individual module control

- Add Manage Modules for all eight hardware modules. Persist enabled state, hide disabled pages, stop their workers and frontend reports, and reject their control requests.
- Withdraw owned hardware settings without deleting user profiles. Remember preferences separately from ownership snapshots and restore them when a module is enabled again.
- Journal incomplete module transitions, finish hardware workers before withdrawal and report failed cleanup with a retry action. Explain the Gaming Mode restart needed to unload an OLED script.
- Rename the About author panel to Author and remove component versions from Included modules.

# 0.5.0 — battery protection and controller motion

- Add firmware battery protection using the kernel's Standard/Fast/Long_Life modes. Preserve the original charging mode, verify writes and retain a durable recovery record across interrupted transactions.
- Add gyro reporting through Lenovo's existing IMU bypass controls and source selection through InputPlumber, preserving unrelated filters and button assignments.
- Add a bounded passive gyro/touchpad test for native and existing virtual controller reports. Stop sampling on page hide, timeout, lease expiry and plugin shutdown.
- Restore saved controls after startup/wake and check for later drift without a continuously running diagnostic reader.
- Extend the standalone-plugin guard, packaging and regression checks to both new modules. Keep pages mounted across native dropdowns.

# 0.4.5 — block overlapping standalone plugins

- Pause all Companion modules when LeGoTDP, LeGo Vibe Control or LeGo2 Brightness Fix is installed, including renamed installations identified by their manifest. WiFi Optimizer and HueSync are not part of this gate.
- Gate startup, migration and every component RPC; check installed plugins every five seconds, and stop running modules on detection. Finish an in-flight operation before unloading workers.
- Show a dedicated paused screen with the conflicting names and uninstall/restart instructions. Stop frontend game reports while blocked and retain user settings.
- Require a fresh Decky/plugin process after resolving a conflict; a blocked instance never runs uninstall hardware restoration for modules it did not start.

# 0.4.4 — keep native dropdowns mounted

- Set the supported Decky alwaysRender flag so native DropdownItem overlays do not unmount the current settings page. Version 0.4.3 only handled visibility changes while mounted.
- Model Decky host unmount behavior in the navigation regression test; retain visibility-gated polling.

# 0.4.3 — preserve the open settings page

- Keep the current section when Steam temporarily hides Quick Access for a dropdown or another overlay; return through All Controls.
- Preserve visibility-based polling and add navigation regression coverage for all seven sections.

# 0.4.2 — reliability and settings recovery

- Harden binary EDID/Lua writes with exclusive random staging files, no-follow checks and directory fsync; preserve Gamescope read access.
- Keep Wi-Fi recovery journals in root-owned persistent storage, migrate outstanding 0.4.1 journals, and retain locks until cancelled workers actually finish.
- Retry failed TDP context changes with backoff; restore the previous effective target when saving a hardware change fails.
- Preserve Extras preferences and game profiles when RyzenAdj is unavailable; expose an explicit download retry.
- Serialize vibration profile mutations and hardware application; reject delayed edits whose game/profile context changed.
- Flush pending RGB, vibration and EPP slider edits when closing controls, and EPP when hiding Quick Access.
- Avoid reloading matching InputPlumber profiles; merge and restore only managed mappings while preserving unrelated changes.
- Cache the Gamescope PID and Lua contents with filesystem change detection; tolerate malformed remapper settings.
- Verify RGB and remapping repeatedly for the first 30 seconds after boot/resume; catch controller initialization that occurs after the first apply, then return to the normal 60-second drift interval.
- Add audit regression tests, journal recovery and concurrency tests, and executable frontend lifecycle checks.

# Changelog

## 0.4.1

- Added F1 through F12 as independent Desktop and Page button actions.
- Added regression coverage for every function-key mapping.

## 0.4.0

- Added a remapper for the two extra left-controller buttons: Desktop and Page.
- Uses SteamOS' existing InputPlumber D-Bus profile API instead of opening or
  grabbing the controller a second time.
- Preserves every unrelated mapping from the active InputPlumber profile and
  restores that exact profile when remapping is disabled or the plugin unloads.
- Added common Steam, keyboard and desktop actions, plus a Disabled option.
- Saves mappings atomically and reapplies them after InputPlumber restarts,
  controller reconnection, console startup and long-running profile drift.
- Validates the Go 2 identity, both source capabilities and the applied profile
  before reporting success.

## 0.3.0

- Added a HueSync-style RGB Lighting page for the Legion Go 2 joystick rings.
- Uses only the native `hid-lenovo-go` effects (Solid, Breathing, Color cycle
  and Rainbow), so animations add no frame-by-frame CPU wakeups.
- Added a separately persistent power-button light toggle, gated to the exact
  83N0/RRCN16WW DSDT layout verified on the target device.
- Corrected the Go 2 power-button mapping from the older HueSync assumption to
  the live BIOS map: ERAM `0xFEEC2300`, offset `0x10`, bit 6.
- Added atomic settings, durable transaction recovery, readback verification,
  startup/resume restoration, minute-level drift correction and restoration of
  the pre-Companion lighting state.

## 0.2.2

- Normalized NetworkManager's escaped BSSID output before verification and
  rollback comparisons.
- Added regression coverage for the exact `AA\:BB\:...` format returned by
  `nmcli -g` on the live Go 2.

## 0.2.1

- Made manual high-band reconnect deterministic by temporarily selecting the
  strongest freshly observed 5/6 GHz BSSID and clearing it immediately after
  the verified connection.
- Added crash-safe rollback for the temporary BSSID and restored the original
  profile and connectivity on every failure path.
- Reduced the common manual action to one scan and one reconnect instead of
  repeated broad, per-band and DFS scans followed by two blind reconnects.

## 0.2.0

- Integrated WiFi Optimizer Go 2 as a fourth isolated Companion module.
- Added a persistent 5/6 GHz preference with 2.4 GHz fallback.
- Added a fresh-scan-only manual reconnect action, bounded to two attempts and
  verified against the actual link frequency.
- Migrated valid orphaned optimizer ownership state without modifying its old
  settings file.
- Hardened WiFi settings and recovery journals and moved regular WiFi status
  work off Decky's shared event loop.
- Reduced the main-menu refresh interval to avoid unnecessary console load.

## 0.1.1

- Made settings writes atomic, recoverable and resistant to unsafe filesystem links.
- Restored the correct TDP and vibration profile after startup, game changes and resume.
- Added periodic CPU, EPP and vibration drift correction without redundant hardware writes.
- Replaced frequent Gamescope process polling with one event-driven property watcher.
- Hardened RPC and stored-profile validation and clarified unreachable display state.
- Reduced duplicate frontend polling and the display fallback wakeup rate.

## 0.1.0

- Combined LeGoTDP, LeGo Vibe Control and LeGo2 Brightness Fix behind one Decky entry point.
- Added the Ayaneo3Companion-style main menu with separate TDP, Vibration, OLED Display and About pages.
- Kept the original background workers, per-game profiles, suspend recovery and hardware restoration behavior.
- Namespaced module settings and RPC methods to prevent collisions.
- Added first-install migration from the three standalone plugin settings directories.
