# Changelog

All notable changes to Legion Go 2 Companion, newest first.

## [Unreleased]

### ✨ Added

- **About:** **Use Rayek's store** makes Rayek's store Decky's store channel after you confirm, so Decky's Store
  tab lists Rayek's plugins and offers their updates.

### 🔧 Changed

**Panel**
- The Decky panel now looks and reads like the other Rayek plugins (same layout, texts and states).
- **B** on a page goes back to All Controls instead of closing the Quick Access menu, and the row you
  came from is selected again.
- Leaving Fan Control with an unsaved curve, or TDP with Custom limits not applied, first asks
  **Discard changes?**; Cancel keeps you on the page with your edits.
- TDP and Vibration show a spinner while they start instead of "Initializing...".
- "Discard unsaved changes?" and "Delete profile for …?" now show Steam's warning style.
- Titles and labels use sentence case ("Hardware controls", "Manage modules", "Enable fan control");
  module names stay as they are.
- Toasts last the same in every module (longer for messages of more than one sentence).

**About**
- Shows the plugin's version and author first, then **Save device report**, and **Updates** at the bottom.
- A saved ZIP or device report shows as `~/…` instead of the full path.

**Updates**
- The update texts match the other plugins ("Installed version X", "Up to date", "Could not complete"
  with the reason).
- **Check for updates** asks GitHub again on every click instead of showing a result up to a minute old.
- A second **Download** while one is running is refused at once ("A download is already running.").
- Messages are clearer: a release that cannot be offered says why, a missing file in the release ZIP
  is named, and a network error says what went wrong.
- A release ZIP is fetched only from GitHub's own download servers.
- If something other than a regular file (such as a link) already has the release ZIP's name in
  Downloads, Companion refuses to save there instead of replacing it.
- When **Check for updates** or **Download** fails, the Desktop app shows why and keeps the last
  result, as the Decky panel does.

**Errors**
- A page that cannot read its status says so with one **Try again** button (it was "Retry status",
  "Retry settings" or "Check again").
- When something fails unexpectedly, the panel and the Desktop app show a short message and the details
  go to the Decky log, never file paths.

**Fan**
- The curve graph and the full screen editor use the shared frame and colors; B is now labeled "Done"
  while editing.

**Wi-Fi**
- When a band change or reconnect is refused, the Desktop app shows the reason in words instead of an
  internal code such as "unsupported_backend".

**Modules**
- Manage Modules also shows the reason in words when the saved band setting cannot be restored while
  turning Wi-Fi on.

### 🐛 Fixed
- **Settings:** a damaged settings file is kept as `.corrupt-<time>` before it is replaced, so it can still be
  looked at.
- **Settings:** settings that could not be read back are never written.

## [2.4.0] - 2026-10-26

### ✨ Added
- **Controller sleep:** under Gyro & touchpad, choose when detached controllers turn themselves off when unused:
  Never, 5, 10, 15, 30, 45 or 60 minutes, for both controllers, as in Legion Space.
- **Controller sleep:** until you pick one, Companion changes nothing and shows **Controller default**; the choice
  survives updates, and turning the module off leaves the controllers at their current time.
- **Controller sleep:** Companion checks your choice every two minutes and after a wake or reconnect and sets it
  only on a controller that reports another value, so a controller asleep when you chose gets it once it wakes.
- **Controller sleep:** available on the Legion Go 2 and on the Legion Go with the 2025 controller firmware (beta).

### 🔧 Changed

**Display**
- Needs SteamOS 3.9 or newer, which supports the Legion Go 2 screen natively; the page now holds only
  **Fix HDR brightness in games**.
- The HDR fix stays because without it games on SteamOS 3.9 still read a 1499-nit HDR peak instead of
  the panel's real values; on older SteamOS the page asks you to update SteamOS, and the fix works there too.
- After the update, Companion removes its old display script by itself (only the exact script it
  installed); a Gaming Mode that is already running keeps it until it restarts.
- If Setup had kept your own script as a `.backup`, it is put back on SteamOS 3.8 and left alone on
  3.9, where it would override SteamOS.
- The original EDID copy that the HDR fix restores is now kept root-only in
  `/var/lib/legiongo2companion/` instead of Decky's settings folder; the old copy moves there by itself.

### 🐛 Fixed

**TDP**
- With **Allow up to 50 W** the boost limits hold again; since 2.3.0 the firmware's custom mode put its own
  limits (37/45 W on the Legion Go 2) back within half a second while the page showed 50 W.
- Companion now leaves the firmware a moment to settle before it sets the boost limits.

**Vibration**
- While a controller sleeps or stops answering, Companion no longer keeps resending your settings to
  it, which could make it buzz now and then.
- After a wake it retries only the settings that did not take, and its periodic check slows from
  every 30 seconds to every 5 minutes until the controller answers or is reconnected.
- Controllers no longer buzz when Companion starts, is updated or uninstalled, or when a controller
  is attached or detached, unless their vibration mode really has to change.
- Companion reads each setting first and writes only one that differs, never one it cannot read
  reliably.
- A setting is confirmed only when the controller reports it twice in a row, so a delayed reply no
  longer counts as a failed write (many "did not confirm" log entries) or as a confirmed one.

**Controller sleep**
- A setting is confirmed only when the controller reports it twice in a row, so a delayed
  reply no longer counts as a failed or confirmed write.

**Controller firmware**
- Check, Download and Install need both controllers attached and say so: "Attach both
  controllers to check or install firmware updates."
- Install still refuses a controller below 30% battery.

**Battery**
- When the firmware turns on fast charging and battery protection at once, the page now says the
  charging mode cannot be read instead of showing "Invalid argument".
- The page offers **Reset charging mode to Standard**, and your saved protection choice is applied
  again afterwards.

**Lighting**
- The stick lights are set again once when Companion starts and once shortly after a wake, so they no
  longer stay wrong when the controller wakes with stale values.
- The stick lights now work while one controller is detached (they failed with "The joystick-ring
  interface does not belong to the expected controller.").
- The stick lights are set again after a controller is attached or detached.

**Fan**
- Fan Control no longer turns itself off for good when the fan briefly returns to firmware control on its
  own (seen once while a controller was reattached).
- Companion takes the fan back after its usual safety checks, up to 3 times in 10 minutes.
- Fan Control still turns off when anything else writes the fan speed, and a single misread of the fan
  setting no longer counts as such a write.

### 🗑️ Removed
- **Display:** display modes (PQ, Gamma 2.2, Hybrid), Setup, Reset display fix, Brightness slider in HDR and the
  Restart Gaming Mode buttons are gone from the panel, Desktop app and Manage Modules (SteamOS 3.9 does it).
- **Display:** turning Display off no longer asks for a Gaming Mode restart.

## [2.3.0] - 2026-10-22

### ✨ Added

**Consoles**
- Companion runs as an early beta on the Legion Go and Legion Go S; each console shows only the
  modules and options that apply to it, in the panel, the Desktop app and Manage Modules.
- Battery, the stick lights and Wi-Fi work on all of them (the stick lights on the Legion Go S need
  its `hid-lenovo-go-s` driver in SteamOS).
- Vibration and Gyro & touchpad work on a Legion Go whose controllers have the 2025 firmware.
- Button mapping, Display, Fan Control and controller firmware stay Legion Go 2 only.
- None of this has been tested on these consoles yet; the README Compatibility table shows what is
  supported, beta or not needed.
- On the Legion Go 2 Z2 (83N1), the power-button light, controller firmware updates and Fan Control
  (beta) now work; Fan Control runs its one-time fan check first. Not tested on an 83N1 yet.

**TDP**
- Uses each console's own firmware limits; **Allow up to 50 W** is offered on the Legion Go (higher risk,
  untested there) but not on the Legion Go S, whose firmware already allows more.
- **Separate battery presets** (under **More**, off by default): Silent, Balanced and Performance use the
  lower limits each console's firmware sets on battery and their usual values on the charger, also in game profiles.
- The Legion Go 2 battery limits are Silent 8/8/20 W, Balanced 16/16/30 W, Performance 20/20/35 W; presets
  switch when you plug in or unplug, and Maximum and Custom are unchanged.

**Lighting**
- The power-button switch now appears on the Legion Go and Legion Go S with a Beta note; it stays
  hidden when the BIOS does not describe the light exactly as expected.

**Vibration**
- On the Legion Go S (beta), **Vibration strength** (Off, Low, Medium, High) and **Touchpad
  vibration**, also per game; the page shows what Companion set because the controller cannot report these.
- On the Legion Go S, Companion sends only the settings you change, so Legion Space's values for
  the others are not overwritten.

**Device report**
- **About → Save device report** (the **Logs** page in the Desktop app) writes a read-only text
  file to your Desktop with hardware and firmware details, firmware tables (DSDT) and EC readings.
- It contains no serial numbers, network names or user names; attach it to a
  [Beta report](README.md#reporting-a-problem-on-legion-go--go-s-beta) from a Legion Go, Legion Go S or new model.

### 🔧 Changed

**Updates**
- Updating from 2.2.x resets the gyro source, battery protection, lighting control and Wi-Fi band
  policy once (Decky runs the old uninstall first); set them again, later updates keep them.
- The gyro source, battery protection, lighting control and Wi-Fi band policy now survive an update
  (the band policy is applied once Wi-Fi has reconnected).

**Display**
- An update reinstalls the script for the panel mode you set up and asks for a Gaming Mode restart only
  when the running Gaming Mode does not already use that exact script (always after updating from 2.1.0 or 2.2.x).
- After updating from 2.1.0, whose script sat in /root where Gaming Mode never read it, the script is
  installed in the right place and the old copy removed; no Setup is needed.

**TDP**
- Presets now come from each console's BIOS table, so every console in the family gets its own firmware
  limits.

**Controller firmware**
- The updater, in the panel and the Desktop app, shows a red **Risk notice** above its
  buttons: it is the only Companion feature that could damage the hardware.
- It has been tested only by reinstalling the firmware already installed, not by updating
  to a newer version; the notice goes away once the next real update has been verified.

### 🐛 Fixed

**Controllers**
- The controller no longer stays dead until a restart, usually after sleep (Legion Go 2, and a
  Legion Go with the 2025 controller firmware), when SteamOS's InputPlumber loses it on a reset or reconnect.
- Companion checks about 17 seconds after a reconnect, a sleep and its own start, and restarts
  InputPlumber once if the controller is still lost; button mapping and gyro source come back on their own.
- That restart happens at most once per 10 minutes and three times per start, and never while
  InputPlumber is stopped or disabled or a firmware update is running.

**TDP**
- **Allow up to 50 W** keeps working after an update without **Retry helper download**; the verified
  `ryzenadj` helper is now kept outside Decky's plugin folder, which every update deletes.
- After updating from 2.2.x the helper is downloaded once in the background at the next start (turning
  **Allow up to 50 W** off cancels it); the firmware limits run until then.
- **Allow up to 50 W** always runs the firmware's custom mode under the higher limits; before, an earlier
  mode (for example Performance) could stay active and replace them.

**Controller firmware**
- Install no longer stops fwupd after a fixed 10 minutes while it is still writing; it
  stops it only after 10 minutes without progress, or 30 in total, asking it to exit cleanly first.

**Install**
- When Decky stops Companion while it is still starting (right after installing a ZIP), Companion now
  stops within Decky's time limit instead of being killed after 5 seconds.

**Startup**
- The panel and the Desktop app wait for the console to be detected instead of briefly showing every
  module.

## [2.2.1] - 2026-10-20

### 🔧 Changed

**TDP**
- With **Allow up to 50 W**, the Legion Go 2 keeps sustained power at its firmware limit (35 W) even when a
  higher value is requested; only the boost limits go to 50 W.
- The panel shows the sustained power that actually runs, and Custom offers sustained power up to 35 W
  with boosts up to 50 W.
- In the Desktop app, choosing **This game** creates the game's profile at once, as in the panel.
- In the Desktop app, the power summary names the game and its preset from the limits that run now and warns
  "above Lenovo's limit" only while 50 W is not allowed.
- In the Desktop app, **Retry helper download** appears only while the helper is missing, and **CPU Boost**
  cannot be switched while its state is unknown.
- The Desktop app shows the "50 W paused", "TDP control unavailable" and "Could not load profile" (with
  **Retry profile**) notices.
- The Desktop app's CPU energy preference uses the panel's names (System default, Prefer CPU, Balanced ·
  Prefer CPU, Balanced · Prefer GPU, Prefer GPU) and stays shown at 50% when the system sets it.
- The Desktop app's readouts are **Limits now** (SPL · SPPT · FPPT) and **Set by**, also on the Overview.

**Fan**
- The 100°C point of a curve now runs at least 2000 RPM (before: 1500 RPM) and stays editable up to full
  speed; at 100°C and above the fan still runs at full speed.
- A saved curve whose 100°C point was lower is raised to 2000 RPM, with the rest of the curve unchanged.
- Dragging a curve point below the minimum running speed stops the fan only when dragged below half of that
  minimum; above it, the point snaps to the minimum. Stepping with the D-pad or keys is unchanged.
- "Assign curves to TDP presets" waits for unsaved curve changes and for a pending curve review.
- The Desktop app uses the panel's names and formats ("Maximum", "50.0%", "Save global curve", "Assign
  curve to …", "Starting curve", "Speed unit", "Speed edit step", "Full screen curve editor").
- The Desktop app can save an unchanged curve to confirm a review or to give a preset its own copy.
- The Desktop app offers "Use global curve" only for a preset that has its own curve, and always lets you
  turn Fan Control off while connected.

**Wi-Fi**
- **Automatic** always means the original Wi-Fi behaviour; if the band setting Companion applied was
  changed outside Companion, choosing Automatic puts the recorded original back (and says so) instead of refusing.
- A saved policy that is not recognized and changed nothing is simply cleared, and a profile that cannot
  be read still refuses.
- Automatic can be chosen whenever there is a saved setting to clear, also on another Wi-Fi card, during
  a recovery or while the link state is unknown.
- In the Desktop app, band choices and the reconnect buttons need a live connection and a supported Wi-Fi
  card, as in the panel.
- The Desktop app shows the panel's notes (settings locked, unsupported card, link state) and reports a
  change it could not verify.

**Display**
- In the Desktop app, all display controls pause while display settings cannot be read, and during an
  unfinished reset only "Retry display reset" can be used (without a second question).
- In the Desktop app, "Brightness slider in HDR" appears only when the screen supports it and no
  restart is pending.
- In the Desktop app, the first mode choice waits for the restart that finishes a reset, and "Reset
  display fix" appears once a mode is chosen.

**Battery**
- In the Desktop app, battery protection shows the saved choice while Companion manages charging and
  waits for a known battery state; controller levels outside 0–100% show "—" (a detached controller is marked).

**Lighting**
- In the Desktop app, effect, colour, brightness and speed wait for the stick lights, and the lights
  show "Restoring saved state…".
- In the Desktop app, "Restore original lighting" stays available while the lights recover.

**Vibration**
- In the Desktop app, Touchpad strength waits for Touchpad vibration, and vibration names match the
  panel (which keeps "Med", "Race" and "Std" on its narrow sliders).

**Button mapping**
- In the Desktop app, the button choices wait for button mapping and are hidden with Restore
  defaults when the controller does not support it.

**Gyro & touchpad**
- In the Desktop app, the gyro source waits for the controller test to end and reads "Not
  confirmed yet…" until Companion has confirmed it; test rates show one decimal.

**Controller firmware**
- In the Desktop app, the install asks "Install controller firmware?".

**Modules**
- In the Desktop app, Manage Modules shows each module's description, error or note as the panel does.

**Settings**
- Settings from the old standalone plugins (LeGoTDP, LeGo Vibe Control, LeGo2 Brightness Fix) and from
  versions before 2.1.0 are no longer imported.
- Companion still pauses while those old plugins are installed.

### 🐛 Fixed

**TDP**
- A first CPU Boost or EPP change that was interrupted part-way (Companion stopped, or the system could not
  confirm the old value afterwards) is now still put back when TDP is turned off or Companion is removed.
- A sustained power (SPL) above 35 W saved by 2.2.0 or earlier, globally or in a game profile, now reads
  as the 35 W that actually runs, in the panel and the Desktop app.
- TDP and battery share one firmware lock, so their firmware calls never overlap.

**Wi-Fi**
- With the iwd backend, when NetworkManager refuses to reconnect a network (seen with a WPA3 profile on a
  mixed WPA2/WPA3 network), Companion asks iwd to rejoin it.
- A 5/6 GHz switch, "Reconnect to 5/6 GHz" or a rollback no longer leaves Wi-Fi disconnected with the
  band policy stuck in recovery.
- Changing the band policy while `/etc/iwd/main.conf` has duplicate `[Rank]` sections or keys is refused
  up front, instead of leaving the band policy stuck in recovery.
- Wi-Fi never writes a line break or other saved text into `/etc/iwd/main.conf` from saved settings.

**Display**
- When another program keeps switching the panel's HDR mode back a moment after Companion sets it,
  Companion now stands off for a minute as intended instead of switching back and forth indefinitely.

**Controller firmware**
- Install refuses a controller that is detached from the console or below 30% battery, and
  checks it again with a fresh reading right before it is written.
- If Install stops there, the message says which controllers were already updated; Check
  refuses to start while another firmware updater is running.

**Modules**
- A module that failed to start can now be turned off from the Decky panel too; its switch reads On
  with the error and **Retry … start**.
- A module that is turned off no longer shows its old startup error.

**Desktop app**
- The Overview TDP badge reads "Off · Lenovo defaults" while TDP control is off and otherwise names
  the preset with its sustained power, as in the panel.
- The TDP page shows "Loading power limits…" instead of placeholder 15/20/25 W values until the
  first reading.
- A change that gets no answer in time now says to check the current value before trying again,
  since it may already have been applied.
- The connection to Companion refuses NaN and Infinity values, which the panels never send.

**Updates**
- About in the Desktop app shows the result of the update check ("Up to date", "No public release yet",
  …) instead of "Not checked".

**Vibration**
- The Desktop app shows "Controller driver not found" when the panel does.
- If the controller does not accept a change, the setting goes back to the value the controller really
  has, instead of showing the new one.
- A change made while no controller is connected now says "Controller not connected. The change is
  saved and applies when it connects." in the panel and the Desktop app, and both show the saved value.

**Startup**
- Modules now start one at a time, which avoids firmware errors when several of them touch the
  hardware right after boot.
- A module whose start takes longer than 15 s no longer holds back the others.
- Until it has finished, the panel and the Desktop app show it as **Starting** ("Still starting. Its
  controls appear when it has finished.") instead of hiding it or reporting an error.
- A module that is still starting can be turned off ("Turns off when it has finished starting."); the
  choice is saved at once and applied right after the start, never in the middle of a hardware change.
- Stopping or removing Companion during such a start no longer lets that start apply changes
  afterwards.

**Fan**
- The BIOS/EC versions Companion has already tested are saved in a way that survives a power cut, so the
  fan self-test is not repeated after one.

**Logs**
- Removing old plugin logs never follows a link out of the log folder, and Companion logs a warning when a
  Decky update stops its unload workaround from applying.

## [2.2.0] - 2026-10-18

### ✨ Added

**Desktop app**
- A native C++/Qt app in the application menu with the same modules, settings and wording as the
  Decky panel, laid out for a mouse and a wide window.
- It uses SteamOS's own Qt 6.9 or newer, so nothing extra is installed, and talks to the running
  Decky plugin so both always show the same state.
- When Desktop Mode starts, Companion starts in the system tray; click the icon to open the window.
  Turning off **Show tray icon** in the app's settings also stops this.
- Uninstalling the plugin closes the app, and updating the plugin restarts it in the new version
  (it asks first if you have unsaved TDP or fan changes).
- Decky's **Install from ZIP** works while the app is open.

**Game profiles**
- The TDP page lists every game that has its own settings, by its Steam name, with what it uses
  (This game, This game + charger, or Global with its settings kept) and its power limits.
- **Edit** opens a game's settings in the same controls, also when the game isn't running, and
  saves them without touching the settings running now; **Delete** removes a profile after a confirmation.

**TDP**
- Custom sliders in the Desktop app follow the same rules as in Decky: boosts never drop below sustained
  power.

**Logs**
- About → View logs shows recent events from Companion's event log with a **Problems** filter.
- The log is `/var/log/legiongo2companion/events.jsonl`, readable by root only, at most 3 MiB.
- The events can be exported for a bug report.
- Warnings now mean something needs attention; expected events such as a Gaming Mode restart are no longer
  reported as problems.

**Controller firmware**
- Check, download and install Lenovo's newest left controller, right controller and
  receiver firmware from Gaming Mode (experimental), asking Lenovo's Legion Space update service.
- Every image is verified with Lenovo's signing key, and everything newer is installed in
  one step with SteamOS's fwupd tool.
- Companion's own controller features pause during the update and the new versions are
  checked afterwards; tested on the Legion Go 2 only by reinstalling the current versions of all three components.
- Installing needs the charger or at least 30% battery; when Lenovo's service answers
  slowly or with errors, a check retries instead of failing.

**Wi-Fi**
- With the WPA backend, the Decky panel can now turn on Only 6 GHz (the Desktop app already could), after
  a confirmation that Wi-Fi is lost outside 6 GHz coverage.
- Only 6 GHz needs NetworkManager 1.58 or newer and is hidden otherwise.

### 🔧 Changed

**Compatibility**
- Tested on SteamOS 3.8 Stable; also checked on SteamOS 3.9.3 Beta, where Display uses SteamOS's
  own Legion Go 2 display profile and Only 6 GHz is available.

**Panel**
- Pages are shorter and easier with the D-pad: explanations moved into the controls' descriptions,
  technical facts into **Show details** at the end of each page, and results into short notifications.
- A failure stays under the control that failed, and every page ends with a row you can always reach.
- Confirmation is asked only for restarting Gaming Mode, Only 5/6 GHz, Only 6 GHz, installing or
  resetting the display fix, deleting a TDP game profile, allowing 50 W and discarding unsaved Custom limits.
- One Fan Control status (for example **Starting your curve…**, **Paused, resuming automatically**,
  **Turned off after an error**) on the page and in the main menu.
- One battery line with both controllers, and a single Wi-Fi **Band** choice.
- Module names are TDP, Fan Control, Vibration, Lighting, Button mapping, Gyro & touchpad, Battery,
  Display and Wi-Fi, with "Gaming Mode" throughout; the Full Power preset is now **Maximum**.

**TDP**
- One **Settings for &lt;game&gt;** choice (Global, This game, This game + charger) replaces the per-game and
  AC toggles.
- **Global keeps the game's settings** for later instead of deleting them; **Delete profile for
  &lt;game&gt;** removes them.
- Presets are buttons that apply at once, Custom limits are a draft with **Apply**, and the power line
  shows what runs now and the live draw.
- Only the Legion Go 2 is supported; TDP, CPU Boost and EPP write nothing on other models and the page says
  the device is unsupported.
- A game's profile applies as soon as the game starts, instead of at the next five-second check.
- The RyzenAdj helper is no longer downloaded at startup; turning on **Allow up to 50 W** downloads and
  verifies it first.
- Inside the normal firmware range a firmware refusal is reported instead of switching to RyzenAdj.
- Disabling the module or uninstalling restores your system's own platform profile, CPU Boost and EPP
  (turning TDP off restores the platform profile), captured before Companion first changes them.
- That capture happens on a fresh install, or after the first reboot following this update; until then the
  previous defaults apply: Balanced, Boost on, balance_performance.

**Fan**
- Custom control now persists: switching between Gaming and Desktop Mode no longer turns Fan Control off.
- After sleep, shutdown, a plugin reload or an update the fan briefly returns to firmware automatic and
  then resumes your saved curve (**Starting your curve…** or **Paused, resuming automatically** meanwhile).
- This includes the first start after updating from 2.1.0 when Fan Control was on.
- The 100°C point's speed is editable; it stays at 100°C, can go down to 1500 RPM and never below the point
  before it, and at 100°C and above the fan still runs at full speed.
- A transient internal error restarts the controller up to three times in ten minutes before Fan Control
  turns itself off with the reason.
- Hardware evidence (fan stall, foreign override, failed readback, invalid sensor value) still turns Fan
  Control off immediately, and a fault in the current boot prevents automatic resume.
- BIOS and EC updates no longer turn Fan Control off; it used to require BIOS RRCN16WW and EC 1.16 exactly
  and now checks the EC fan interface's behaviour at every start.
- After a firmware update, a one-time check raises the fan speed briefly and watches the fan follow; if it
  does not, Fan Control turns off and says the BIOS/EC version changed how the fan is controlled.
- Turning Fan Control on again repeats that check, and a passed check is remembered for that BIOS/EC pair;
  the 83N0 model requirement is unchanged.
- An unstable EC read or selector interference now counts as a transient error, retried up to three times
  in ten minutes, instead of turning Fan Control off at once.
- The fan controller and its backup now release the EC on SIGTERM and are protected from the
  out-of-memory killer.
- A leftover override without a live owner is released to firmware automatic instead of blocking Fan
  Control until power-off.

**Lighting**
- The power-button light survives BIOS updates; it no longer requires BIOS RRCN16WW with an exact ACPI
  table, and instead checks that the live table still places the light at the same register bit (83N0 only).

**Vibration**
- Disabling the module or uninstalling restores the controller's own vibration settings captured
  before the first change on a fresh install; existing installs keep restoring the default profile.

### 🐛 Fixed

**Display**
- Your 2.1.0 panel mode now actually loads; the display script moves automatically to the Gaming Mode
  user's folder when the plugin starts, and one Gaming Mode restart applies it.
- Hybrid no longer fights another HDR tool; when something keeps switching the screen back, it stops
  after four undone switches in either direction instead of blanking the screen every few seconds.
- Disabling Display in Hybrid no longer switches through PQ on the way out.
- After a failed screen read or a resume, Hybrid keeps forwarding brightness for the rest of the Gaming
  Mode session.
- After an interrupted EDID write, the fix continues once gamescope publishes its EDID at a new path
  and the old file is gone.
- Turning the EDID fix off restores the original only into the file Companion trimmed, never into a new
  file gamescope published in its place.
- Uninstalling clears the HDR setting only when you had chosen an HDR mode, and in Desktop Mode Display
  no longer polls the Gaming Mode screen.

**Fan**
- Starting Fan Control no longer occasionally waits 20 seconds and counts as a failed attempt.
- Saving an unsaved curve right after turning Fan Control on or off is no longer refused as "changed
  elsewhere".
- With the TDP module turned off, power-preset curve assignments use the global curve.
- A full runtime directory can no longer keep the custom fan speed after Fan Control stops; the fan returns
  to firmware automatic first and the stop reason is recorded only if it can be.
- The independent backup also retries a release that failed once when the main fan process is already gone.

**TDP**
- A charger change that fails part-way is retried, instead of the battery limits being written back over
  the charger limits; a game switch right after plugging in applies the charger settings.
- A limit the firmware keeps clamping is retried three times, not again every 30 seconds.
- Extras taps and helper retries apply in the order you made them.

**Game profiles**
- Saving a game profile no longer drops profiles beyond the 512th, for example ones imported from
  LeGoTDP; the limit only applies to new profiles.

**Wi-Fi**
- A disconnected device shows Disconnected, so Only 5/6 GHz and Only 6 GHz can be turned off while they
  leave you without Wi-Fi.
- Turning a band policy off also works after a failed reconnect, and with iwd it is never blocked by the
  saved-network check.
- Reconnect works again after an automatic rollback; an open saved network no longer blocks the iwd
  features, and `/etc/iwd/main.conf` stays intact when its last section has no final newline.
- The backend is read from NetworkManager's effective configuration, so a configuration override is no
  longer misread; without a link, Wi-Fi no longer runs `nmcli` in the background.

**Lighting**
- The first time Companion takes over the lights it keeps their brightness, and it writes settings
  even when the controller's current value can't be read.

**Button mapping**
- It can be disabled while InputPlumber is unavailable or stalled, the restored system actions
  are verified, and it accepts an InputPlumber profile with no mappings.

**Vibration**
- One failed controller event no longer stops Vibration from reapplying your settings when a
  controller reconnects.

**Install**
- Stopping, reloading or updating the plugin no longer takes five seconds and a forced kill on Decky
  3.2.10.

**Panel**
- Pages stop reading hardware while the Companion tab is hidden, and the check for conflicting plugins no
  longer rereads every plugin's manifest every five seconds.
- A momentary error in that check no longer blocks the panel until Decky restarts.

**Logs**
- Fewer warnings in the system log for expected events; events from the plugin's first seconds are kept.

## [2.1.0] - 2026-10-11

### ✨ Added

**Fan**
- New Fan Control: build your own cooling curve in the Steam overlay with direct control through the
  console's embedded controller (EC), with live temperature and measured fan speed beside the editor.
- An eight-point curve from 0 to 100°C; adjust the first seven points, the last stays fixed at 100°C and
  full speed. Points stay in temperature order and the curve cannot decrease in speed as temperature rises.
- Edit by touch (drag points) or with the controller: press **A** on the graph, select a point with
  **LB / RB**, and use the **D-pad** to change temperature and speed; **B** leaves point editing.
- Choose a speed edit step, or open the **full-screen curve editor** for more room.
- Choose RPM or percentage and see the actual measured fan speed separately from your requested curve;
  live readings also work with firmware automatic control, and 100% is the tested 4800 RPM ceiling.
- Built-in hysteresis avoids chasing small temperature fluctuations; speed rises quickly when needed, then
  falls more gradually as the device cools.
- Three starting curves (Silent, Balanced and Aggressive) are editable; new settings use Balanced with one
  global curve, choosing a template changes the draft, and saving keeps it for later.
- Optionally assign curves to Companion's TDP presets, including game and battery/AC profile changes;
  assignments start off and do not follow the separate SteamOS QAM power profiles.
- You choose when custom cooling runs: saving a curve while control is off does not activate it, and
  turning Fan Control off returns control to firmware automatic.
- After startup, plugin reload, sleep or a fault, custom control needs explicit re-enabling; saved curves
  remain, and the page reports blocked operation or incomplete recovery.
- A separate watchdog helps release custom control back to firmware automatic if the controller stops
  responding; stale or inconsistent readings and detected faults also trigger recovery.
- This recovery depends on a working kernel and EC connection.
- Fan Control is limited to the validated Legion Go 2 8ASP2 / 83N0, BIOS RRCN16WW and EC 1.16; other
  hardware or firmware variants are not enabled.
- Supported targets are **0 RPM (stop)** or **1500–4800 RPM**, and measured speed can vary from the
  target.
- Use only one fan controller at a time, and return any other controller to firmware automatic before
  enabling Fan Control.

### 🔧 Changed
- **Vibration:** rapid edits now keep the latest queued value for each control and profile instead of replaying
  superseded values behind a slow hardware write; closing the page still saves the final accepted change.
- **Wi-Fi:** power-save changes and recovery verify the active network profile, reducing the chance of restoring a
  previous connection's policy after switching networks; unverified recovery remains pending.
- **Display:** invalid OLED mode requests are rejected before changing settings or installing the legacy display
  script, instead of silently selecting PQ.
- **Docs:** the README now explains controls, recovery and hardware limits, with screenshots of all nine modules,
  module management and the full-screen fan editor.

## [2.0.0] - 2026-10-10

### ✨ Added
- **Lighting:** joystick rings also work on 8AHP2 / 83N1 (not only 8ASP2 / 83N0) when the controller interface is
  present ([PR #2](https://github.com/Rayekkk/LegionGo2Companion/pull/2) by [Andrii Dmytrenko (@Antti)](https://github.com/Antti)).
- **Lighting:** an 8AHP2 owner reported successful RGB and sleep/wake recovery; this is RGB support, not full
  validation of every feature on that variant, and power-button lighting stays limited to the audited 83N0.
- **Lighting:** RGB has a preview for both joystick rings.

### 🔧 Changed

**Panel**
- A new vertical menu uses native Decky controls, monochrome icons and Steam's familiar focus styling,
  and all ten pages remain accessible with the controller.
- The overview shows TDP limits, vibration and touchpad settings, lighting, Wi-Fi and both controller
  battery levels.
- Charging status and current vibration settings come first on their pages; technical driver details can
  be expanded when needed.

**Wi-Fi**
- The page puts the verified connection, network name, band and active backend first, with actions suited
  to the backend SteamOS is using.
- Automatic WiFi leaves band selection to the system. With IWD, Prefer 5/6 GHz keeps 2.4 GHz as a
  fallback, while Only 5/6 GHz excludes it.
- Companion automatically looks for higher bands and recovers these IWD policies after startup or wake.
- With WPA, a separate action makes a one-time attempt to connect on 6 GHz; it does not enable continuous
  automatic band switching on WPA.

**TDP**
- Updated presets are Silent, Balanced, Performance and Full Power; saved Maximum selections become Full
  Power.
- Advanced TDP Control stays enabled by default; turn it off for a simpler Custom mode with one power
  slider and automatic burst limits, and the optional 50 W unlock remains available.

**Vibration**
- Vibration intensity, mode and touchpad intensity keep their familiar stepped sliders.

**Display**
- When SteamOS provides its own Legion Go 2 display profile, Companion retires its older brightness fix
  and uses the native support.
- EDID correction remains available independently, with clear notices when cleanup or a Gaming Mode
  restart is needed.

### 🐛 Fixed

**Panel**
- Entering a page focuses Back and returning selects the module you opened; status refreshes and closing
  a dropdown no longer move your selection.
- Long controller paths fit inside the expanded driver details.
- Slow hardware or network reads no longer hold up unrelated controls, and status updates are shared where
  possible, with fewer duplicate reads and no busy loop during shutdown.

**Errors**
- TDP summaries wait for confirmed profile settings, and failed status reads show an error and retry
  action instead of an endless spinner or misleading old values.

**Wi-Fi**
- Connection results now check the actual link, band and selected profile before reporting success;
  reopening a page, roaming or changing the backend cannot leave old actions or results active.
- Interrupted reconnections and failed band changes recover more reliably.
- Switching to native WPA clears inactive IWD policy changes without starting another manager or
  reconnecting a forgotten network.

**TDP**
- TDP and CPU edits stay attached to the correct game and battery/AC profile; missing profiles load fresh
  global settings, and delayed replies cannot overwrite a newer game or slider choice.
- CPU controls and recovery remain accessible when TDP support is unavailable or applying a profile fails.
- Disabling modules or uninstalling restores managed power, Boost and EPP settings, and reports any
  incomplete recovery.

**Settings**
- Slider, battery and gyro-source changes accepted before closing a page are saved; reopening waits
  for those saves, so older reads cannot undo your latest choice.
- Settings recovery and restoration are stricter.

**Vibration**
- Tests use confirmed intensity, ignore repeated taps and stop the effect reliably.
- Profile changes, resets and reconnects no longer reuse stale game settings.

**Lighting**
- RGB recovers from temporary controller failures.

**Button mapping**
- Remapping restores each button's captured system action, including custom mappings, without
  changing unrelated buttons.

**Display**
- Brightness and HDR changes follow the current display and SteamOS session after docking or a
  compositor restart; restore, reset and uninstall avoid applying an old display's settings to a new one.
- Interrupted display changes keep their recovery data.

**Modules**
- A failed startup is visible in module management and can be retried.
- Pending changes finish before module shutdown or restoration; failed module starts are clearly
  reported, with a retry after partial startup work is cleaned up.

**Updates**
- Update and optional RyzenAdj downloads handle failures and cancellation more reliably.

## [1.0.4]

### ✨ Added
- **Wi-Fi:** an Only 5/6 GHz policy that disables 2.4 GHz while leaving both higher bands available, with a
  confirmed connection attempt when an access point is missing from the scan.

### 🔧 Changed
- **Wi-Fi:** better reconnection pacing, live band verification, and rollback when the selected 5/6 GHz policy
  cannot connect.
- **Lighting:** no more RGB writes after a failed controller command, and the failure is reported in module status.

### 🐛 Fixed
- **Lighting:** delayed RGB restoration after transient controller write failures during startup on SteamOS beta
  with Linux 6.18.

## [1.0.3]

### ✨ Added
- **Display:** Reset Display Fix restores brightness, HDR and EDID handling, removes Companion's display script and
  returns OLED Display to its initial setup.

### 🔧 Changed
- **CPU:** EPP profile labels are replaced with Prefer CPU / Prefer GPU and clearer descriptions.
- **Display:** interrupted display resets retain recovery data and can be retried safely.

### 🐛 Fixed
- **CPU:** System default EPP on SteamOS 3.8 Stable, and recovery of inconsistent CPU policy values after a failed
  default change.
- **CPU:** switching from System default to an explicit EPP preset matching its current hardware value.

## [1.0.2]

### ✨ Added
- **Battery:** separate battery levels and connection states for the left and right Legion Go 2 controllers,
  including the last controller-reported level while detached.

### 🔧 Changed
- **Panel:** informational panels now take controller focus, so the Decky panel scrolls fully with the joystick.

## [1.0.1]

### 🔧 Changed
- **CPU:** better EPP compatibility.
- **Wi-Fi:** better preference recovery when switching SteamOS versions.
- **Compatibility:** support for SteamOS 3.10 with Linux kernel 7.2 is retained.

### 🐛 Fixed
- **Compatibility:** Button mapping, Lighting, and Gyro & touchpad now work on SteamOS 3.8 Stable with Linux
  kernel 6.x.

## [1.0.0]

### ✨ Added
- **Plugin:** first release.
