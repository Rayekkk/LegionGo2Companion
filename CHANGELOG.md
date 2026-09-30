# Unreleased

### Added

- Support joystick-ring RGB control on Legion Go 2 8AHP2 / 83N1 through the existing verified controller interface, including saved lighting restoration after wake. Device-tested on SteamOS 3.9.1 Preview (20260914.100); power-button lighting retains its separate hardware and firmware restrictions.
- Detect gamescope's native Legion Go 2 display profile by the presence of its system Lua file. Automatically retire Companion's legacy display fix, remove its owned Lua file, disable the obsolete display controls, and report cleanup or Gaming Mode restart requirements.
- Add Advanced TDP Control, enabled by default. When disabled, Custom uses one 5–35 W TDP slider and targets SPPT at TDP +10 W (up to 37 W) and FPPT at TDP +15 W (up to 45 W). The existing 50 W unlock remains available.

### Changed

- Replace the TDP presets with Silent 8/15/20 W, Balanced 16/25/30 W, Performance 20/32/35 W, and Full Power 35/37/45 W; remove Minimum. Existing saved "max" selections continue as Full Power.
- Reorder the TDP page: Enable, game profile, current limits, presets, Custom controls, CPU Power Controls, then Extras.
- Show the active TDP mode and all three power limits in the main menu. The Battery summary also shows left and right controller charge levels, and Vibration shows whether touchpad vibration is on.

### Improved

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
