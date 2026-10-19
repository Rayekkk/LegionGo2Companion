# 2.2.1

Not released yet.

### Fixed

- **Controller firmware:** Install refuses a controller that is detached from the console or below 30% battery, and checks it again with a fresh reading right before that controller is written; if it stops there, the message says which controllers were already updated. Check refuses to start while another firmware updater is running.
- Wi-Fi never writes a line break or other saved text into `/etc/iwd/main.conf` from saved settings.
- **Wi-Fi with the iwd backend:** when NetworkManager refuses to reconnect a network (seen with a WPA3 profile on a mixed WPA2/WPA3 network), Companion asks iwd to rejoin it. A 5/6 GHz switch, "Reconnect to 5/6 GHz" or a rollback no longer leaves Wi-Fi disconnected and the band policy stuck in recovery.
- **Display:** when another program keeps switching the panel's HDR mode back a moment after Companion sets it, Companion now stands off for a minute as intended instead of switching back and forth indefinitely.
- **Wi-Fi:** changing the band policy while `/etc/iwd/main.conf` has duplicate `[Rank]` sections or keys is refused up front, instead of leaving the band policy stuck in recovery.
- Removing old plugin logs never follows a link out of the log folder.
- Companion logs a warning when a Decky update stops its unload workaround from applying.
- **TDP:** a first CPU Boost or EPP change that was interrupted part-way (Companion stopped, or the system could not confirm the old value afterwards) is now still put back when TDP is turned off or Companion is removed.
- **Desktop app:** a change that gets no answer in time now says to check the current value before trying again, since it may already have been applied.

### Changed

- **Fan Control:** the 100°C point of a curve now runs at least 2000 RPM (before: 1500 RPM); its speed stays editable between 2000 RPM and full speed in both panels. A saved curve whose 100°C point was lower is raised to 2000 RPM, with the rest of the curve unchanged. At 100°C and above the fan still runs at full speed.
- **TDP, Allow up to 50 W:** the Legion Go 2 keeps sustained power at its firmware limit (35 W) even when a higher value is requested; only the boost limits go to 50 W. The panel now shows that instead of the requested sustained value, and Custom offers sustained power up to 35 W with boosts up to 50 W.
- **Fan Control** works and reads the same in the Decky panel and the Desktop app:
  - Dragging a curve point below the minimum running speed stops the fan only when dragged below half of that minimum; above it, the point snaps to the minimum. Stepping with the D-pad or keys is unchanged.
  - The Desktop app uses the panel's names and formats ("Maximum", "50.0%", "Save global curve", "Assign curve to …"), can save an unchanged curve to confirm a review or to give a preset its own copy, offers "Use global curve" only for a preset that has its own curve, and always lets you turn Fan Control off while connected.
  - "Assign curves to TDP presets" waits for unsaved curve changes and for a pending curve review in both.
- **TDP and CPU** follow the same rules and texts in the Decky panel and the Desktop app. In the Desktop app: choosing **This game** creates the game's profile at once (before, only the first Apply or preset did); the power summary names the game and its preset from the limits that run now and warns "above Lenovo's limit" only while 50 W is not allowed; **Retry helper download** appears only while TDP or the 50 W unlock is missing the helper; the panel's "50 W paused", "TDP control unavailable" and "Could not load profile" (with **Retry profile**) notices are shown; **CPU Boost** cannot be switched while its state is unknown; the CPU energy preference uses the panel's names (System default, Prefer CPU, Balanced · Prefer CPU, Balanced · Prefer GPU, Prefer GPU) and stays shown at 50% when the system sets it; the readouts are **Limits now** (SPL · SPPT · FPPT) and **Set by**, also on the Overview.
- **Wi-Fi** follows the same rules and texts in the Decky panel and the Desktop app: band choices and the reconnect buttons need a live connection and a supported Wi-Fi card in both, the Desktop app shows the panel's notes (settings locked, unsupported card, link state) and reports a change it could not verify, and **Automatic** can clear a saved setting whenever there is one to clear, also on another Wi-Fi card, during a recovery or while the link state is unknown.
- **Battery, Lighting, Vibration, Button mapping, Gyro & touchpad, Controller firmware, Display and Manage Modules** follow the same rules and texts in the Decky panel and the Desktop app:
  - **Manage Modules:** a module's switch shows whether it is turned on, so a module that failed to start reads On with its error and **Retry … start**, and can now be turned off from the Decky panel too. Both show the module's description, error or note.
  - **Display** in the Desktop app: while display settings cannot be read or a reset is unfinished, only the reset can be used ("Retry display reset", without a second question); "Brightness slider in HDR" appears only when the screen supports it and no restart is pending; the first mode choice waits for the restart that finishes a reset; "Reset display fix" appears once a mode is chosen.
  - Also in the Desktop app: Battery protection shows the saved choice while Companion manages charging and waits for a known battery state, and controller levels outside 0–100% show "—" (a detached controller is marked); the lighting effect, colour, brightness and speed wait for the stick lights, "Restore original lighting" stays available while the lights recover, and the stick lights show "Restoring saved state…"; Touchpad strength waits for Touchpad vibration; the button choices wait for button mapping and are hidden with Restore defaults when the controller does not support it; the gyro source waits for the controller test to end and reads "Not confirmed yet…" until Companion has confirmed it; test rates show one decimal; the firmware install asks "Install controller firmware?".
  - The Decky panel and the Desktop app use the same vibration names (the panel keeps "Med", "Race" and "Std" on its narrow sliders).
- **Wi-Fi:** **Automatic** always means the original Wi-Fi behaviour. If the band setting Companion applied was changed outside Companion, choosing Automatic now puts the recorded original back (and says so) instead of refusing. If a saved policy is not recognized and Companion never changed anything for it, Automatic simply clears it. A profile that cannot be read still refuses, because the original could not be checked.
- Settings from the old standalone plugins (LeGoTDP, LeGo Vibe Control, LeGo2 Brightness Fix) and from versions before 2.1.0 are no longer imported. Companion still pauses while those plugins are installed.
- Internal cleanup: duplicated and unused code was removed across the backend and both interfaces, with no change in what Companion does.

# 2.2.0

Released 2026-10-18.

Tested on SteamOS 3.8 Stable. Compatibility was also checked on SteamOS 3.9.3 Beta, where Display uses SteamOS's own Legion Go 2 display profile and Only 6 GHz is available.

### New

#### Desktop app

- **Legion Go Companion in Desktop Mode.** A native C++/Qt app in the application menu with the same modules, settings and wording as the Decky panel, laid out for a mouse and a wide window. It uses SteamOS's own Qt 6.9 or newer; nothing extra is installed. It talks to the running Decky plugin, so both always show the same state.
- **Ready in the tray.** When Desktop Mode starts, Companion starts in the system tray; click the icon to open the window. Turning off **Show tray icon** in the app's settings also stops this.
- **Game profiles at a glance.** The TDP page lists every game that has its own settings, by its Steam name, with what it uses (This game, This game + charger, or Global with its settings kept) and its power limits. **Edit** opens a game's settings in the same controls, also when the game isn't running, and saves them without touching the settings running now; **Delete** removes a profile after a confirmation.
- TDP Custom sliders follow the same rules as in Decky: boosts never drop below sustained power.
- Uninstalling the plugin closes the app; updating the plugin restarts it in the new version (it asks first if you have unsaved TDP or fan changes). Decky's **Install from ZIP** works while the app is open.
- **Logs** (About → View logs) shows recent events from Companion's event log (`/var/log/legiongo2companion/events.jsonl`, readable by root only, at most 3 MiB) with a **Problems** filter and can export them for a bug report. Warnings now mean something needs attention; expected events such as a Gaming Mode restart are no longer reported as problems.

<a href="docs/screenshots/desktop-tdp.png"><img src="docs/screenshots/desktop-tdp.png" alt="Desktop app TDP page with power, CPU and game profiles" width="760"></a>

<a href="docs/screenshots/desktop-fan.png"><img src="docs/screenshots/desktop-fan.png" alt="Desktop app Fan Control page with live readings and the curve editor" width="760"></a>

#### Controller firmware (experimental)

- **Check, download and install Lenovo controller firmware from Gaming Mode.** Companion asks Lenovo's Legion Space update service for the newest left controller, right controller and receiver firmware, verifies every image with Lenovo's signing key, and installs everything newer in one step with SteamOS's fwupd tool. Its own controller features pause during the update and the new versions are checked afterwards. Tested on the Legion Go 2 by reinstalling the current versions of all three components.
- Installing needs the charger or at least 30% battery. Lenovo's service sometimes answers slowly or with errors; a check retries those replies instead of failing.

#### Wi-Fi

- **Only 6 GHz from the panel.** With the WPA backend, the Decky panel can now turn on Only 6 GHz (the Desktop app already could), after a confirmation that Wi-Fi is lost outside 6 GHz coverage. The option needs NetworkManager 1.58 or newer and is hidden otherwise.

### Changed

#### Decky panel

- **Shorter pages, easier with the D-pad.** Explanations moved into the controls' descriptions, technical facts into **Show details** at the end of each page, and results into short notifications; a failure stays under the control that failed. Every page ends with a row you can always reach.
- **Confirmation only where it matters:** restarting Gaming Mode, Only 5/6 GHz, Only 6 GHz, installing or resetting the display fix, deleting a game's TDP profile, allowing 50 W, and discarding unsaved Custom limits. Everything else applies without asking.
- **TDP:** one **Settings for &lt;game&gt;** choice (Global, This game, This game + charger) replaces the per-game and AC toggles. **Global keeps the game's settings** for later instead of deleting them; **Delete profile for &lt;game&gt;** removes them. Presets are buttons that apply at once; Custom limits are a draft with **Apply**. The power line shows what runs now and the live draw.
- **Clearer status:** one Fan Control status (for example **Starting your curve…**, **Paused, resuming automatically**, **Turned off after an error**) on the page and in the main menu; one battery line with both controllers; a single Wi-Fi **Band** choice.
- **Names:** TDP, Fan Control, Vibration, Lighting, Button mapping, Gyro & touchpad, Battery, Display, Wi-Fi; "Gaming Mode" throughout. The Full Power preset is now **Maximum**.

<a href="docs/screenshots/overview.png"><img src="docs/screenshots/overview.png" alt="Decky panel main menu with a status line for every module" width="360"></a> <a href="docs/screenshots/tdp.png"><img src="docs/screenshots/tdp.png" alt="Decky TDP page with preset buttons and power status" width="360"></a>

#### Fan Control

- **Custom control now persists.** Switching between Gaming and Desktop Mode no longer turns Fan Control off. After sleep, shutdown, a plugin reload or an update the fan briefly returns to firmware automatic and then resumes your saved curve; the page shows **Starting your curve…** or **Paused, resuming automatically** meanwhile. This includes the first start after updating from 2.1.0 when Fan Control was on.
- **The 100°C point's speed is editable.** It stays at 100°C, can go down to 1500 RPM and never below the point before it, and shapes the curve's top end. At 100°C and above the fan still runs at full speed.
- **Clearer failure handling.** A transient internal error restarts the controller up to three times in ten minutes before Fan Control turns itself off with the reason. Hardware evidence (fan stall, foreign override, failed readback, invalid sensor value) still turns it off immediately, and a fault in the current boot prevents automatic resume.
- **BIOS and EC updates no longer turn Fan Control off.** Fan Control used to require BIOS RRCN16WW and EC 1.16 exactly. It now checks the EC fan interface's behaviour at every start instead. After a firmware update, a one-time check raises the fan speed briefly and watches the fan follow. If it does not, Fan Control returns to firmware automatic, turns off and says the BIOS/EC version changed how the fan is controlled; turning it on again repeats the check. A passed check is remembered for that BIOS/EC pair. The 83N0 model requirement is unchanged.
- **Momentary EC access errors are retried.** An unstable EC read or selector interference now counts as a transient error, retried up to three times in ten minutes, instead of turning Fan Control off at once.
- **Safer process shutdown.** The fan controller and its backup now release the EC on SIGTERM and are protected from the out-of-memory killer. A leftover override without a live owner is released to firmware automatic instead of blocking Fan Control until power-off.

#### RGB Lighting

- **The power-button light survives BIOS updates.** It no longer requires BIOS RRCN16WW with an exact ACPI table. Companion now checks that the live ACPI table still places the light at the same register bit, on the 83N0 model only.

#### TDP and CPU

- **Legion Go 2 only.** TDP, CPU Boost and EPP write nothing on other models and the page says the device is unsupported.
- **A game's profile applies as soon as the game starts**, instead of at the next five-second check.
- **RyzenAdj only when you ask for it.** The helper is no longer downloaded at startup; turning on **Allow up to 50 W** downloads and verifies it first. Inside the normal firmware range a firmware refusal is reported instead of switching to RyzenAdj.
- **Disabling the module or uninstalling restores your system's own platform profile, CPU Boost and EPP** (turning TDP off restores the platform profile), captured before Companion first changes them: on a fresh install, or after the first reboot following this update. Until then the previous defaults apply: Balanced, Boost on, balance_performance.

#### Vibration

- Disabling the module or uninstalling restores the controller's own vibration settings captured before the first change on a fresh install; existing installs keep restoring the default profile.

### Fixes

#### Display

- **Your 2.1.0 panel mode now actually loads.** 2.1.0 saved the display script where gamescope never read it. The script now moves automatically to the Gaming Mode user's folder when the plugin starts; restart Gaming Mode once to apply.
- **Hybrid no longer fights another HDR tool.** When something keeps switching the screen back, Hybrid stops after four undone switches in either direction instead of blanking the screen every few seconds. Disabling Display in Hybrid no longer switches through PQ on the way out.
- After a failed screen read or a resume, Hybrid keeps forwarding brightness for the rest of the Gaming Mode session.
- After an interrupted EDID write, the fix continues once gamescope publishes its EDID at a new path and the old file is gone.
- Turning the EDID fix off restores the original only into the file Companion trimmed, never into a new file gamescope published in its place.
- Uninstalling clears the HDR setting only when you had chosen an HDR mode. In Desktop Mode, Display no longer polls the Gaming Mode screen.

#### Fan Control

- Starting Fan Control no longer occasionally waits 20 seconds and counts as a failed attempt.
- Saving an unsaved curve right after turning Fan Control on or off is no longer refused as "changed elsewhere".
- With the TDP module turned off, power-preset curve assignments use the global curve.
- A full runtime directory can no longer keep the custom fan speed after Fan Control stops: the fan returns to firmware automatic first and the stop reason is recorded only if it can be. The independent backup also retries a release that failed once when the main fan process is already gone.

#### TDP and CPU

- A charger change that fails part-way is retried, instead of the battery limits being written back over the charger limits. A game switch right after plugging in applies the charger settings.
- A limit the firmware keeps clamping is retried three times, not again every 30 seconds.
- Extras taps and helper retries apply in the order you made them.
- Saving a game profile no longer drops game profiles beyond the 512th, for example ones imported from LeGoTDP; the limit only applies to new profiles.

#### Wi-Fi

- **A disconnected device shows Disconnected**, so Only 5/6 GHz and Only 6 GHz can be turned off while they leave you without Wi-Fi. Turning a band policy off also works after a failed reconnect, and with iwd it is never blocked by the saved-network check.
- Reconnect works again after an automatic rollback. An open saved network no longer blocks the iwd features, and `/etc/iwd/main.conf` stays intact when its last section has no final newline.
- The Wi-Fi backend is read from NetworkManager's effective configuration, so a configuration override is no longer misread. Without a link, Wi-Fi no longer runs `nmcli` in the background.

#### Lighting and controllers

- The first time Companion takes over the lights it keeps their brightness, and it writes settings even when the controller's current value can't be read.
- Button mapping can be disabled while InputPlumber is unavailable or stalled, and the restored system actions are verified. It also accepts an InputPlumber profile with no mappings.
- One failed controller event no longer stops Vibration from reapplying your settings when a controller reconnects.

#### Responsiveness and recovery

- **Stopping, reloading or updating the plugin no longer takes five seconds and a forced kill** on Decky 3.2.10.
- Pages stop reading hardware while the Companion tab is hidden. The check for conflicting plugins no longer rereads every plugin's manifest every five seconds.
- A momentary error in that check no longer blocks the panel until Decky restarts.
- Fewer warnings in the system log for expected events; events from the plugin's first seconds are kept.

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
