# 2.1.0

Released 2026-10-11.

### New: Fan Control

Build your own cooling curve directly in the Steam overlay, with direct control
through the console's embedded controller (EC). Live temperature and measured fan
speed sit alongside the editor.

- **An eight-point curve from 0 to 100°C.** Adjust the first seven points; the last stays fixed at 100°C and full speed. Points stay in temperature order, and the curve cannot decrease in speed as temperature rises.
- **Touch and controller editing.** Drag points with touch, or press **A** on the graph, select a point with **LB / RB**, and use the **D-pad** to change temperature and speed. **B** leaves point editing. Choose a speed edit step, or open the **full-screen curve editor** for more room.
- **RPM or percentage.** Choose the display unit and see actual measured fan speed separately from your requested curve. Live readings also work with firmware automatic control; 100% corresponds to the tested 4800 RPM ceiling.
- **Steadier cooling:** built-in hysteresis avoids chasing small temperature fluctuations. Speed rises quickly when needed, then falls more gradually as the device cools.
- **Three starting curves.** Silent, Balanced and Aggressive provide editable starting points. New settings use Balanced with one global curve; choosing a template changes the draft, and saving keeps it for later.
- **Optional power-preset assignments.** Keep the same global curve everywhere, or enable assignments to Companion's TDP presets, including game and battery/AC profile changes. Assignments start off and do not follow the separate SteamOS QAM power profiles.
- **You choose when custom cooling runs.** Saving a curve while control is off does not activate it. Enable Fan Control when ready; turning it off returns control to firmware automatic. After startup, plugin reload, sleep or a fault, custom control requires explicit re-enabling. Saved curves remain available, and the page reports blocked operation or incomplete recovery.
- **Independent recovery monitoring:** a separate watchdog helps release custom control back to firmware automatic if the controller stops responding. Stale or inconsistent readings and detected faults also trigger recovery; this depends on a working kernel and EC connection.

**Compatibility:** Fan Control is limited to the validated **Legion Go 2 8ASP2 /
83N0, BIOS RRCN16WW and EC 1.16**. Supported targets are **0 RPM (stop)** or
**1500–4800 RPM**; measured speed can vary from the target. Other hardware or
firmware variants are not enabled for this feature. Use only one fan controller at
a time, and return any other controller to firmware automatic before enabling it.

#### Fan Control in the Steam overlay

<a href="docs/screenshots/fan-control.png"><img src="docs/screenshots/fan-control.png" alt="Fan Control showing live temperature, measured fan speed and the saved curve" width="760"></a>

#### Full-screen curve editor

<a href="docs/screenshots/fan-curve-editor.png"><img src="docs/screenshots/fan-curve-editor.png" alt="Full-screen fan curve editor with eight temperature and speed points" width="760"></a>

### Fixes and improvements

- **More responsive vibration edits:** rapid changes now keep the latest queued value for each control and profile, instead of replaying superseded values behind a slow hardware write. Closing the page still saves the final accepted change.
- **Wi-Fi recovery follows the connection:** power-save changes and recovery verify the active network profile, reducing the chance of restoring a previous connection's policy after switching networks. Unverified recovery remains pending.
- **Stricter OLED mode validation:** invalid mode requests are rejected before changing settings or installing the legacy display script, instead of silently selecting PQ.
- **A clearer user guide:** the README now explains controls, recovery and hardware limits, with screenshots of all nine modules, module management and the full-screen fan editor.

# 2.0.0

Released 2026-10-10.

### New and changed

#### Interface and navigation

- A new vertical menu uses native Decky controls, monochrome icons and Steam's familiar focus styling. All ten pages remain accessible with the controller.
- The overview shows TDP limits, vibration and touchpad settings, lighting, Wi-Fi and both controller battery levels. Charging status and current vibration settings come first on their pages; technical driver details can be expanded when needed.

#### Wi-Fi

- Wi-Fi now puts the verified connection, network name, band and active backend first, with actions suited to the backend SteamOS is using.
- Automatic WiFi leaves band selection to the system. With IWD, Prefer 5/6 GHz keeps 2.4 GHz as a fallback, while Only 5/6 GHz excludes it. Companion automatically looks for higher bands and recovers these IWD policies after startup or wake.
- With WPA, a separate action makes a one-time attempt to connect on 6 GHz. This does not enable continuous automatic band switching on WPA.

#### TDP, CPU and game profiles

- Updated presets: Silent, Balanced, Performance and Full Power. The old Minimum preset is removed, and saved Maximum selections become Full Power.
- Advanced TDP Control remains enabled by default. Turn it off for a simpler Custom mode with one power slider and automatic burst limits; the optional 50 W unlock remains available.

#### Controllers, RGB and battery

- **RGB compatibility fix from [PR #2](https://github.com/Rayekkk/LegionGo2Companion/pull/2), contributed by [Andrii Dmytrenko (@Antti)](https://github.com/Antti):** joystick-ring control now accepts 8AHP2 / 83N1 as well as 8ASP2 / 83N0 when the required controller interface is present. An 8AHP2 owner reported successful RGB and sleep/wake recovery. This is RGB support, not full validation of every feature on that variant; power-button lighting remains limited to the audited 83N0 hardware and firmware.
- RGB has a preview for both joystick rings. Vibration intensity, mode and touchpad intensity keep their familiar stepped sliders.

#### OLED and native SteamOS support

- When SteamOS provides its own Legion Go 2 display profile, Companion retires its older brightness fix and uses the native support. EDID correction remains available independently, with clear notices when cleanup or a Gaming Mode restart is needed.

### Fixes

#### Navigation and status

- Entering a page focuses Back; returning selects the module you opened. Status refreshes and closing a dropdown no longer move your selection. Long controller paths fit inside the expanded driver details.
- TDP summaries wait for confirmed profile settings. Failed status reads show an error and retry action instead of an endless spinner or misleading old values.

#### Wi-Fi

- Connection results now check the actual link, band and selected profile before reporting success. Reopening a page, roaming or changing the backend cannot leave old actions or results active.
- Interrupted reconnections and failed band changes recover more reliably. Switching to native WPA clears inactive IWD policy changes without starting another manager or reconnecting a forgotten network.

#### TDP, CPU and game profiles

- TDP and CPU edits stay attached to the correct game and battery/AC profile. Missing profiles load fresh global settings, and delayed replies cannot overwrite a newer game or slider choice.
- CPU controls and recovery remain accessible when TDP support is unavailable or applying a profile fails. Disabling modules or uninstalling restores managed power, Boost and EPP settings, while reporting any incomplete recovery.

#### Controllers, RGB and battery

- Slider, battery and gyro-source changes accepted before closing a page are saved. Reopening waits for those saves, so older reads cannot undo your latest choice.
- Vibration tests use confirmed intensity, ignore repeated taps and stop the effect reliably. Profile changes, resets and reconnects no longer reuse stale game settings.
- RGB recovers from temporary controller failures, and button remapping restores each button's captured system action, including custom mappings, without changing unrelated buttons.

#### OLED display

- Brightness and HDR changes follow the current display and SteamOS session after docking or a compositor restart. Restore, reset and uninstall avoid applying an old display's settings to a new one.
- Interrupted display changes keep their recovery data. Failed startup is visible in module management and can be retried.

#### Responsiveness and recovery

- Slow hardware or network reads no longer hold up unrelated controls. Status updates are shared where possible, with fewer duplicate reads and no busy loop during shutdown.
- Pending changes finish before module shutdown or restoration. Failed module starts are clearly reported, with a retry after partial startup work is cleaned up.
- Update and optional RyzenAdj downloads handle failures and cancellation more reliably. Settings recovery, restoration and package checks are stricter; outdated build dependencies and unused code have been removed.

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
