# Legion Go 2 Companion 2.2.0

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

<a href="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/desktop-tdp.png"><img src="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/desktop-tdp.png" alt="Desktop app TDP page with power, CPU and game profiles" width="760"></a>

<a href="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/desktop-fan.png"><img src="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/desktop-fan.png" alt="Desktop app Fan Control page with live readings and the curve editor" width="760"></a>

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

<a href="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/overview.png"><img src="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/overview.png" alt="Decky panel main menu with a status line for every module" width="360"></a> <a href="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/tdp.png"><img src="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/2.2.0/docs/screenshots/tdp.png" alt="Decky TDP page with preset buttons and power status" width="360"></a>

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
