# Native Desktop app

Since 2.2 the plugin ships **Legion Go Companion**, a native Desktop Mode app
(C++17 / Qt Quick/QML) next to the Decky panel. Both are front ends to the same
Python backend.

## One engine, two panels

The existing Python backend is the single authority for hardware, module
lifecycle, persisted settings, game profiles, fan curves and ownership. Decky
hosts this backend. Both the Decky menu and the C++17 / Qt Quick/QML
Desktop application call its guarded RPCs. Desktop does not create another
hardware settings database, EC controller or enforcement loop. It requires
running Decky. Local tray/window preferences only control presentation.

## Shared rules

What the two panels derive from backend state (which actions are offered, status
headlines and labels, the TDP slider coupling, Fan curve editing) is written once
in plain ES modules, `desktop/native/<module>.mjs`, with `.d.mts` type files for
TypeScript. The Decky pages import them from `src/*.tsx` and rollup bundles them;
the QML pages import the same files (`import "tdp.mjs" as Tdp`), which
`desktop/native/CMakeLists.txt` lists among the QML module files; the frontend
tests load the real files. The backend still enforces every limit; these modules
only decide what the panels show and offer. Keep them free of DOM and Qt types,
and remember that Qt list values are not JavaScript arrays.

## Pages

The sidebar has Overview, TDP, Fan Control, Controllers, Lighting, Battery,
Display, Wi-Fi, Modules and About. Overview shows battery, performance/cooling,
Wi-Fi and controller readbacks. **Controllers** combines vibration, button
mapping, gyro/touchpad and controller firmware in independently gated sections.
**About → View logs** opens the log browser ([Diagnostic logging](LOGGING.md)).
Wide windows use two columns; narrower windows stack the sections. Diagnostic
details are collapsed initially. The app renders native Qt controls and a Qt
Quick Shapes fan plot; there is no browser, WebEngine, Electron or Python GUI
dependency. The application uses a shared flat dark palette.
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
getters; writes 155 seconds). Writes are never replayed automatically. Reconnection reads the newly
published private session. Check Decky reads systemd status; Start Decky is
available only in Desktop Mode for a stopped/failed service with MainPID=0,
and uses the system's authorization dialog. It never kills or restarts an
active loader. A hung active service requires separate diagnosis because
Decky 3.2.10 has a known shutdown/EOF failure.

Uninstall removes the managed launcher/menu registration; a running app notices
that its executable was deleted and quits. A plugin update replaces the
executable; the app restarts itself in the new version once the new registration
is ready, and asks first if a TDP or fan draft is unsaved. Unload alone removes
the session, retaining the registration for a later reconnect. Tray preferences and unrelated user files
are retained. Failure/uninstall tests use isolated fixtures; routine UI testing
does not crash Decky or uninstall the user's working plugin.

Backend startup registers **Legion Go Companion** in the actual non-root Decky
user's KDE application menu. It does not launch a root GUI, install system
packages, add a daemon, download Qt at runtime or require sudo in the launcher.

- Menu entry: `~/.local/share/applications/legiongo2companion.desktop`.
- Private marked launcher, icon, runtime pointer and 0600 session file:
  `~/.local/share/legiongo2companion/`.
- Native executable and its hash manifest ship in the plugin's `dist/desktop/`
  directory. Backend startup copies the verified executable into the private
  directory above and the app runs that copy, so updating the plugin in place
  never touches a running file. Qt is not bundled; the app links the system Qt (see below).
- Unload removes the session capability; uninstall removes only marked app
  files/menu entry, preserving unrelated user files and Companion settings.

Root publication pins directory handles, rejects unsafe parents/symlinks and
unmarked files, verifies the executable digest and restores its execute bit when
a Windows-created ZIP loses POSIX modes. The user launcher invokes the native
ELF with only the session **path** in its arguments, never the capability.

The loopback RPC bridge listens on `127.0.0.1` at an ephemeral port. An explicit
method contract derived from Decky panels (`desktop/native/rpc-methods.json`,
77 methods, generated and checked by `scripts/desktop-contract.mjs`), a private
rotating capability in the 0600 session file,
exact Origin/Host, argument binding and bounded request admission protect it.
There is no static web server, GET mutation, CORS endpoint or LAN listener.
The C++ transport rejects redirects/proxies and oversized responses. Admitted
backend transactions are shielded from UI disconnection and drained on unload.
Root and compromised processes running as the same desktop user are outside
this capability boundary; it does not claim isolation from the desktop account.

## System Qt

The app is built against **Qt 6.9** (the minimum; SteamOS 3.8 ships Qt 6.9.1 as
part of Plasma: qt6-base, qt6-declarative, qt6-wayland, qt6-svg) and links the
system libraries in `/usr/lib` at runtime. Nothing overrides the library, plugin
or QML import paths, and the executable carries no RUNPATH. Portability rules:

- public Qt API only (no private headers or `Qt::*Private` targets) and
  unversioned QML imports;
- QML is compiled to bytecode only (`--only-bytecode`): the binary links no
  QML AOT helpers, and a newer engine recompiles the embedded sources;
- Qt's `qt_version_tag` symbol makes the loader refuse a Qt older than 6.9.

`desktop/launch.py` checks the installed Qt before starting the app and shows a
dialog (kdialog, or stderr) when Qt 6 is missing or older than 6.9. Qt 7 would
not provide `libQt6Core.so.6` and is reported as missing. The Decky panel does
not depend on Qt.

CI builds with the official Qt 6.9.1 SDK (Ubuntu 24.04: glibc 2.39 and GCC 13,
both older than SteamOS 3.8's glibc 2.41 / libstdc++ GLIBCXX_3.4.34), runs the
native tests, then runs the same binaries again on the newest Qt 6 runtime
(currently 6.11.2) with `LD_BIND_NOW=1`, so a missing symbol fails the job.

## Reproduce

Use Node 24, Python 3.10+, CMake 3.24+, Ninja, a C++17 compiler and a Qt 6.9.x
SDK (e.g. `aqt install-qt linux desktop 6.9.1 linux_gcc_64`).

```bash
npm ci --ignore-scripts
npm run typecheck
npm run test:frontend
python -W error -m unittest discover -s tests -p 'test_*.py'
node scripts/desktop-contract.mjs --check
npm run build
python scripts/build-native.py --qt /path/to/Qt/6.9.1/gcc_64 --build /tmp/companion-native --test --bundle dist/desktop
node scripts/check-desktop-build.mjs --runtime
npm run package -- --check
npm run package -- --output /absolute/fresh/path/desktop-candidate.zip
python scripts/verify-package.py /absolute/fresh/path/desktop-candidate.zip
```

`npm run package` takes the tracked files from the committed `HEAD` (`git archive`)
and only the built `dist/` from the checkout; it refuses uncommitted changes to
packaged files. Names are sorted, every entry carries the commit date and fixed
modes, so the same commit, `dist/` and Node version give the same SHA-256.

`build-native.py --test` runs the CTest suites `transport`, `logviewer`,
`native_host` and `native_ui` (QML against a fixture backend);
`tests/native_capture.py` renders every page against a loopback fake backend.
Python tests exercise the real shared backend behind both Decky calls and
Desktop dispatch. CI repeats the native tests on the newest Qt 6 (see above).
Physical gamepad navigation, suspend/resume and hardware changes still need
testing on the console; fixture screenshots are not Legion measurements.
