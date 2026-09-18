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
