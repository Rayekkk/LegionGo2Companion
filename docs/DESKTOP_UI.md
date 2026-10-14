# Native Desktop UI — local 2.2 experiment

The manifest remains **2.1.0**. This is unreleased development.
Initial development was local. Native builds have since been deployed and
validated on the Legion in Desktop and Gaming Mode; see the validation notes below.

## One engine, two panels

The existing Python backend is the single authority for hardware, module
lifecycle, persisted settings, game profiles, fan curves and ownership. Decky
hosts this backend. Both the unchanged Decky menu and the C++17 / Qt Quick/QML
Desktop application call its guarded RPCs. Desktop does not create another
hardware settings database, EC controller or enforcement loop. It requires
running Decky. Local tray/window preferences only control presentation.

The native UI covers TDP, Fan, vibration, lighting, buttons, gyro/touchpad,
battery, OLED, Wi-Fi, module management and updates in the approved sidebar
layout. Overview shows battery, performance/cooling, Wi-Fi and controller
readbacks. The legacy Display page is retained unchanged. It renders local
native Qt controls and a Qt Quick Shapes fan plot;
there is no browser, WebEngine, Electron or Python GUI dependency.

The desktop sidebar starts with Overview. Controller combines vibration, lighting,
button mapping and gyro/touchpad settings in independently gated sections. Wide
windows use two columns; narrower windows stack the sections. Diagnostic details
are collapsed initially. The application uses a shared flat dark palette.
TDP limits and RGB parameters use sliders. TDP presets, EPP profiles and lighting
effects are directly visible choices. Numeric EPP uses a 0–100% preference slider
when supported by the driver; its value is converted to the backend's 0–255 range.
RGB includes a local color preview. RGB/EPP sliders commit once on release (or
after keyboard input settles); TDP edits require Apply. Changing the profile,
losing availability or receiving an external value cancels a pending slider edit.

Both panels reload saved state from the backend. Fan and overview normally poll
once per second, other panels every three seconds; requests never overlap.
Controller diagnostics use one-second status reads while the other Controller
sections retain a three-second cadence. Successful edits refresh those sections.
This is polling, not an instantaneous push channel. A saved Desktop curve is
visible on Decky's next existing poll (normally about one second). An unsaved
editor draft remains local until Save. Fan revisions reject concurrent stale
writes; external changes preserve an open draft and require reloading it before
saving. Successful writes are not automatically retried after connection loss.

Desktop and QAM maintain separate visibility leases on the shared TDP readback
loop. Closing one panel cannot clear the other panel's lease. Minimizing/hiding
Desktop stops hardware polls; a hidden tray app checks only connection and
session context every ten seconds. Backend enforcement and guardians continue.
Closing Desktop releases its panel lease and diagnostics, not the plugin.

## Native interaction and module rules

Mouse/touch drag and keyboard editing are available on the graph. Focus alone
does not edit: Enter/Space activates, arrows change temperature/speed, Q/E or
Page Up/Down select points, Escape ends editing. The editor has a separate
fullscreen native window using the same draft and selected curve. Physical
Steam Input/controller bindings still require device testing; keyboard tests
are not proof of physical LB/RB input.

The UI reads curve bounds, templates, units and assignments from the backend.
Eight ordered points, monotonic RPM, the fixed 100°C maximum, real stop versus
automatic release, supported firmware and independent guardians retain their
existing backend policies. Opening either UI does not arm custom Fan Control.
All nine modules retain their existing module gates, locks and restoration.

Desktop reads the authoritative running game from the backend instead of
submitting Steam foreground-app hints. Per-game/AC writes use existing context
checks; an old draft cannot be silently applied to a newly foregrounded game.
OLED session changes target Gaming Mode/Gamescope. Desktop never restarts KDE;
without Gamescope those controls explain that Gaming Mode is required.

## Installation and local transport

Module state is read before component getters. Turning off one Controller
feature hides only that section's controls; the other sections remain usable.
Off, restoration pending, unavailable and disconnected states are distinct.
Old sensor samples are not displayed as current after a module is disabled.

Desktop Mode supports the KDE system tray. App settings control icon visibility
and optional close-to-tray; the tray menu shows/hides the window or quits the
app. If the icon becomes unavailable, a hidden window is restored. A second
launch shows the existing window through a user-only local socket.

Loss of the shared backend pauses controls and preserves unsaved drafts. Reads
use bounded deadlines (4 seconds for connection/context, 20 seconds for other
getters). Writes are never replayed automatically. Reconnection reads the newly
published private session. Check Decky reads systemd status; Start Decky is
available only in Desktop Mode for a stopped/failed service with MainPID=0,
and uses the system's authorization dialog. It never kills or restarts an
active loader. A hung active service requires separate diagnosis because
Decky 3.2.10 has a known shutdown/EOF failure.

Uninstall removes the managed launcher/menu registration. An already running
window detects its removal, stops RPC admission, withdraws the tray icon and
shows a reinstall message. Unload alone removes the session, retaining the
registration for a later reconnect. Tray preferences and unrelated user files
are retained. Failure/uninstall tests use isolated fixtures; routine UI testing
does not crash Decky or uninstall the user's working plugin.

Backend startup registers **Legion Go Companion** in the actual non-root Decky
user's KDE application menu. It does not launch a root GUI, install system
packages, add a daemon, download Qt at runtime or require sudo in the launcher.

- Menu entry: `~/.local/share/applications/legiongo2companion.desktop`.
- Private marked launcher, icon, runtime pointer and 0600 session file:
  `~/.local/share/legiongo2companion/`.
- Native executable, dynamically replaceable Qt libraries, QML plugins and
  license inventory: the plugin's `dist/desktop/` directory.
- Unload removes the session capability; uninstall removes only marked app
  files/menu entry, preserving unrelated user files and Companion settings.

Root publication pins directory handles, rejects unsafe parents/symlinks and
unmarked files, verifies the executable digest and restores its execute bit when
a Windows-created ZIP loses POSIX modes. The user launcher invokes the native
ELF with only the session **path** in its arguments, never the capability.

The loopback RPC bridge listens on `127.0.0.1` at an ephemeral port. An explicit
74-method contract derived from Decky panels, private rotating capability,
exact Origin/Host, argument binding and bounded request admission protect it.
There is no static web server, GET mutation, CORS endpoint or LAN listener.
The C++ transport rejects redirects/proxies and oversized responses. Admitted
backend transactions are shielded from UI disconnection and drained on unload.
Root and compromised processes running as the same desktop user are outside
this capability boundary; it does not claim isolation from the desktop account.

## Reproduce

Use Node 24, Python 3.10+, CMake 3.24+, Ninja, a C++17 compiler and the official
Qt **6.11.2** SDK (Base, Declarative, SVG, Wayland and ICU 73.2). The license
preparation script uses `requests`, available in the isolated aqt development
environment; it is not a plugin runtime dependency.

```bash
npm ci --ignore-scripts
npm run typecheck
npm run test:frontend
python -W error -m unittest discover -s tests -p 'test_*.py'
npm run build
python scripts/build-native.py --qt /path/to/Qt/6.11.2/gcc_64 --build /tmp/companion-native --test
python scripts/prepare-native-licenses.py --output /tmp/companion-qt-licenses
python scripts/bundle-native.py --exe /tmp/companion-native/legiongo2companion --qt /path/to/Qt/6.11.2/gcc_64 --licenses /tmp/companion-qt-licenses --output /tmp/companion-runtime
# Replace dist/desktop with the fresh runtime, retaining rpc-methods.json from build.
node scripts/check-desktop-build.mjs --runtime
npm run package -- --check
npm run package -- --output /absolute/fresh/path/desktop-candidate.zip
python scripts/verify-package.py /absolute/fresh/path/desktop-candidate.zip
```

Normal CMake builds compile QML ahead of time. `scripts/build-native-cross.py`
is an optional Windows-to-Linux cross-build using matching official Qt SDKs,
Zig and GCC 12 headers/runtime link inputs. It embeds QML for runtime compilation,
not CMake's AOT cache. It does not redistribute GCC link inputs or install them
on SteamOS. Its successful x86-64 ELF build proves compilation only.

The bundle manifest records exact file hashes and external system libraries
(graphics, font, X11/Wayland, glibc and libstdc++). Those libraries and actual
Wayland/X11 execution must be checked on SteamOS. The application contains no
download/install workaround for missing system libraries.

Qt/ICU notices, corresponding upstream source locations and upstream SDK SBOMs
are included. Before any future public binary release, provide the matching Qt
source/build material with the binary download as described in SOURCE.md.

## Validation boundaries

Local Windows MSVC builds and native Qt tests exercise session validation,
authenticated RPC, redirect rejection, curve input/bounds, drafts/revisions,
game transitions, panel leases, hidden polling and no automatic write retry.
The executable is also rendered against a loopback fake backend on all ten
pages. Screenshots contain fixture data, not Legion measurements.

The native bundle now includes Qt Widgets for the system tray. Archive download
limits are 48 MiB in both existing updater paths; the 128 MiB expansion limit,
member/path checks, checksum verification and host restrictions are unchanged.

Python tests separately exercise the **real shared Fan backend** behind both
public Decky calls and Desktop dispatch: identical curve/unit readback, one
persisted state, stale revision rejection and no implicit Fan enablement.
Existing Decky regression/type/build and dependency/package checks remain.

POSIX installer/ownership tests skip on this Windows host and are present in
Linux CI. No remote CI has run for these uncommitted changes.

The first live SteamOS validation on 2026-10-12 confirmed the same running
Decky system service across the user's Gaming-to-Desktop-to-Gaming transition, native
Wayland rendering, available system libraries, private per-user registration,
and all nine module status/read APIs. Twenty-five isolated Linux Desktop tests
passed without platform skips. Controlled saves of an inactive Fan curve and
RPM/% preference reached the common settings manager; a second client saw the
same revision, stale saves were rejected, and the original values were restored
(only the revision advanced). Opening the app did not enable custom Fan control.
Bidirectional saves through the authenticated Desktop bridge and public Decky
RPC returned identical settings after the return to Gaming Mode. Native transport
and QML regression tests also ran on SteamOS: six transport and twenty-six UI
test results passed. QML tests use a fixture backend; live captures separately
exercise real module read APIs without issuing hardware changes.

This is an initial runtime validation, not a full hardware regression: physical
gamepad navigation, suspend/resume, active Fan failure injection, all hardware
mutations and Gaming/AC profile transitions still require separate live tests.
Native captures and getter results do not establish those effects. The Desktop
client continues to depend on the Decky-hosted engine being running.

## Roadmap

Connection, service-action and QML source-location diagnostics are described in
[Diagnostic logging](LOGGING.md), alongside the shared backend and independent
worker logs. Native logs never copy backend replies or session capabilities.

- **2.2:** native Desktop app and shared backend, then live SteamOS validation.
- **2.3:** controller firmware updater after separate LegionSpace evidence and
  safety work; this experiment adds no flashing implementation.
- **3.0 tentative:** broader Legion Go models and possible rename; deferred.

Overview uses the sidebar for page navigation and has no Quick Access row. Display separates legacy correction from EDID for games: confirmed upstream display support replaces legacy controls with a green status, while EDID remains available through the same shared backend. Unconfirmed retired support is shown as a warning, not a confirmed upstream fix.
