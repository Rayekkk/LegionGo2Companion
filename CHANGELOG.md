# Unreleased

### Fixed

- Drain completed RPC and hardware-worker snapshots explicitly, avoiding a busy loop during module shutdown when asyncio completes a gather before deferred cleanup callbacks run.
- Clear or roll back an inactive iwd preference after a native SteamOS WPA switch without starting a competing manager or reconnecting a forgotten profile; retain watchdogs when recovery is incomplete.
- Show the verified WiFi connection, SSID, band and backend first, expose backend-specific actions, and distinguish inactive saved policies from enforced settings.
- Invalidate WiFi actions/results after failed reads, native backend/profile changes, roaming and page reopen; verify the final state before showing success and pause automatic radio work while WPA is selected.
- Discover higher bands automatically on the Legion Go 2 iwd preference path after startup, activation and resume, with bounded scans/reconnections and cooperative cancellation.
- Require the requested NetworkManager profile, connected device state and physical radio link before reporting WiFi success; reject ambiguous iwd profile ownership and recover interrupted reconnections independently of the plugin process.
- Retry valid failed WiFi rollback journals, invalidate stale recovery checkpoints after restoring configuration, and migrate the retired wake-dispatcher flag without discarding profile ownership.
- Offer a one-time 6 GHz connection on the native WPA backend, continue targeted 6 GHz discovery when a scan only finds 5 GHz, and clear the temporary AP pin after verifying the actual link while preserving failed-recovery watchdogs.
- Preserve unreadable legacy WiFi recovery journals instead of migrating an error marker; isolate offline recovery and plugin-inventory tests from live Linux system directories.
- Preserve battery and gyro-source changes accepted before page close, including changes still waiting for a status read, and keep reopened snapshots behind their complete mutation tail.
- Wait for accepted vibration slider saves before loading settings on a reopened page.
- Apply the same mutation ordering to main-menu battery, gyro, remapper, RGB and vibration summaries; keep battery transaction verification independent of its public read barrier.
- Run update-publication close barriers outside the event loop during conflict detection and unload, preserving completion through repeated cancellation.
- Recheck the live output and bound compositor session during final legacy OLED withdrawal, preserving pending cleanup when output identity cannot be verified.
- Validate each RyzenAdj HTTPS redirect before opening its destination, rejecting foreign hosts, URL credentials and nonstandard ports.
- Preserve remapper mutation order across page instances and wait for accepted remapper/RGB edits before sampling a reopened page.
- Keep vibration initial-load retries behind all remaining reads from a failed attempt, while displaying the first error immediately.
- Resolve WiFi connection UUIDs from the selected interface, so an external adapter cannot supply another profile; reuse captured interface discovery and check named profile activity independently of listing order.
- Reject nonregular plugin manifests before reading and open settings nonblocking to prevent FIFO substitution from hanging metadata verification.
- Report WiFi rollback-lock failures without synchronous settings I/O on the event loop or masking the original error.
- Load final remapper ownership inside its operation lock during unload and uninstall, preventing stale restoration while keeping settings I/O off the event loop.
- Read console and controller battery status independently, so slow or failed controller sampling cannot hold console controls or duplicate outstanding reads after a console error.
- Match vibration-test endpoints to the discovered HID identity, revalidate reused event nodes and the opened evdev descriptor, and probe only matching endpoints.
- Keep vibration-test I/O in one worker transaction that stops, erases and closes the effect before releasing its busy guard, including repeated cancellation and failed starts.
- Keep module status responsive during slow hardware reads and pending restoration without weakening mutation, conflict or shutdown gates.
- Run WiFi power-save transactions, device discovery, startup commands and legacy restoration outside the event loop; finish cancelled transactions before releasing the network lock.
- Move controller monitor settings recovery and file-lock waits off the event loop.
- Debounce RGB speed gestures, save the final value on close and ignore obsolete or repeated timer callbacks.
- Serialize Gaming Mode restart clicks with module changes and reject actions from discarded module-management pages.
- Prevent discarded About-page callbacks from starting new update requests while preserving requests already accepted by the plugin session.
- Ignore superseded or repeated CPU EPP debounce callbacks while preserving save-on-close.
- Keep the complete OLED property watcher cache when safety checks require a fresh compositor read.
- Recheck the live connector, HDR content and compositor session before legacy HDR changes, hand-back, display reset or uninstall restoration; stable Hybrid ticks still reuse the watcher.
- Refresh reopened module pages as soon as an older status read finishes, and remove the extra three-second wait after a verified WiFi reconnect.
- Reject new hardware gestures from unmounted WiFi, display, remapper, lighting and vibration pages while still saving previously accepted edits on close.
- Move module status probes and durable module-choice commits off the event loop, preserving completion through cancellation.
- Coalesce vibration test taps and finish pending slider saves before testing their confirmed intensity.
- Adopt committed vibration reset settings even when the subsequent hardware apply fails.
- Reject vibration heartbeats that predate a committed edit or profile change, and deliver a game change immediately after an older busy request finishes.
- Confirm fresh global TDP settings after capability changes, keep CPU and helper recovery available after an auto-apply failure, and reject callbacks retained from an earlier game editor.
- Revalidate live OLED output ownership before brightness hand-back or a delayed backlight write after docking/HDR changes.
- Coalesce superseded RGB callbacks and preserve newer slider drafts when older writes finish or fail.
- Drain hardware workers after failed module resume and normal shutdown, including unload errors and repeated cancellation of the drain itself.
- Wait for the current game's profile before admitting TDP/CPU edits, reload global limits for missing profiles, and keep existing CPU AC profiles accessible without a TDP provider.
- Replace and restore complete remapper YAML names, including multiline scalars, and separate restored mappings without a final newline.
- Verify compositor-session ownership before restoring legacy OLED brightness or HDR baselines.
- Reject stale brightness/Hybrid writers after a compositor restart and seed new vibration profiles from one admitted running-game snapshot.
- Serialize vibration profile transitions against new gestures, adopt the committed profile scope even after hardware failures, and reject over-capacity profile creation without truncating saved data.
- Persist legacy OLED brightness and HDR recovery baselines atomically before changing compositor state, and surface failed OLED startup to module management for retry.
- Restore the available balanced firmware profile even when some PPT capabilities cannot be read, while still requiring readback confirmation.
- Resolve the vibration edit/reset profile once, serialize reset behind pending slider edits, and discard delayed replies from a previous game or edit.
- Keep TDP mutations and refreshes tied to their game context, show recoverable initial TDP/Vibration read errors, and expose actual TDP availability independently of CPU controls.
- Separate preserved remapper blocks when the captured YAML has no final newline.
- Restore each dedicated button's captured system mapping for Default, including custom actions or an absent mapping, while preserving unrelated live mappings.
- Share verified TDP/CPU cleanup between module disable and uninstall, including CPU Boost and EPP managed only by game or AC profiles; report partial failures without skipping other controls.
- Expose failed module starts separately from saved enable choices, block their RPCs and offer a safe retry after draining partial startup work.
- Show the first OLED, RGB, remapper and WiFi status error with a retry action instead of an indefinite spinner.
- Download the optional RyzenAdj helper outside the hardware admission gate with transfer deadlines, cancellation and partial-file cleanup.
- Expire stale frontend game reports and resolve current Steam processes for vibration hotplug/resume and TDP profiles.
- Confirm fresh vibration writes with bounded readback before reporting success or caching them.
- Remove the vulnerable Rollup cleanup dependency chain, update brace-expansion, and validate the generated Decky bundle and package build inputs.

### Added

- Support joystick-ring RGB control on Legion Go 2 8AHP2 / 83N1 through the existing verified controller interface, including saved lighting restoration after wake. An 8AHP2 owner reported successful RGB control and sleep/wake recovery on SteamOS 3.9.1 Preview (20260914.100); this is owner-reported validation, not a maintainer test of that variant. Power-button lighting retains its separate hardware and firmware restrictions.
- Detect gamescope's native Legion Go 2 display profile by the presence of its system Lua file. Automatically retire Companion's legacy display fix, remove its owned Lua file, disable the obsolete display controls, and report cleanup or Gaming Mode restart requirements.
- Add Advanced TDP Control, enabled by default. When disabled, Custom uses one 5–35 W TDP slider and targets SPPT at TDP +10 W (up to 37 W) and FPPT at TDP +15 W (up to 45 W). The existing 50 W unlock remains available.

### Changed

- Replace the TDP presets with Silent 8/15/20 W, Balanced 16/25/30 W, Performance 20/32/35 W, and Full Power 35/37/45 W; remove Minimum. Existing saved "max" selections continue as Full Power.
- Reorder the TDP page: Enable, game profile, current limits, presets, Custom controls, CPU Power Controls, then Extras.
- Show the active TDP mode and all three power limits in the main menu. The Battery summary also shows left and right controller charge levels, and Vibration shows whether touchpad vibration is on.

### Improved

- Run independent module RPCs concurrently while retaining per-resource ordering and lifecycle barriers; share only in-flight conflict scans, without caching completed admission checks.
- Keep EDID correction available independently of display-mode setup and the installed Lua script.
- Keep native-display detection independent of the Lua file's contents, gamescope version, and SteamOS update channel.
- Remove unreachable updater helpers, legacy backend methods, unused frontend code, and obsolete package entries after a focused dead-code review.
- Avoid duplicate TDP and vibration startup reads, overlapping CPU-control reads, redundant guard metadata probes and WiFi settings reads, and unnecessary InputPlumber capability queries during remapper discovery.

# 1.0.4

### Added

- Added an Only 5/6 GHz Wi-Fi policy that disables 2.4 GHz while leaving both higher bands available, with a confirmed connection attempt when an access point is missing from the scan.

### Fixed

- Fixed delayed RGB restoration after transient controller write failures during startup on SteamOS beta with Linux 6.18.

### Improved

- Improved Wi-Fi reconnection pacing, live band verification, and rollback when the selected 5/6 GHz policy cannot connect.
- Stopped additional RGB writes after a failed controller command and report the failure in module status.

# 1.0.3

### Added

- Added Reset Display Fix to restore brightness, HDR and EDID handling, remove Companion's display script and return OLED Display to its initial setup.

### Fixed

- Fixed System default EPP on SteamOS 3.8 Stable and recovery of inconsistent CPU policy values after a failed default change.
- Fixed switching from System default to an explicit EPP preset matching its current hardware value.

### Improved

- Replaced EPP profile labels with Prefer CPU / Prefer GPU and clearer descriptions.
- Interrupted display resets retain recovery data and can be retried safely.

# 1.0.2

### Added

- Added separate battery levels and connection states for the left and right Legion Go 2 controllers, including the last controller-reported level while detached.

### Improved

- Informational panels now participate in controller focus, allowing the Decky panel to scroll fully with the joystick.
- Improved reliability of the Windows settings crash-recovery checks used by the quality workflow.

# 1.0.1

### Fixed

- Fixed compatibility of Button Remapper, RGB Lighting, and Gyro & Touchpad on SteamOS 3.8 Stable with Linux kernel 6.x.
- Improved CPU EPP compatibility and Wi-Fi preference recovery when switching SteamOS versions.
- Retained support for SteamOS 3.10 with Linux kernel 7.2.

# 1.0.0

Initial release.
