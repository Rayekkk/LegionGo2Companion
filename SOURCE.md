# Frontend source and relinking

The generated `dist/index.js` includes code from `@decky/api` 1.1.3 under
LGPL-2.1. `npm run package` includes its full package source in
`THIRD_PARTY_SOURCES/decky-api-1.1.3/` and its license in
`THIRD_PARTY_LICENSES/decky-api-LGPL-2.1.txt` inside the ZIP archive. These
generated directories are not part of the Git checkout.

The source and build inputs for the combined frontend are included in the
release archive as `src/`, `scripts/`, `rollup.config.js`, `tsconfig.json`,
`package.json` and `package-lock.json`. With Node.js 18 or newer, rebuild the bundle with:

```bash
npm ci --ignore-scripts
npm run build
```

This permits replacing or modifying `@decky/api` and relinking the frontend.
The local Rollup configuration is adapted from `@decky/rollup` 1.0.2 under
BSD-3-Clause (`LICENSE.DECKY-ROLLUP`). It preserves Decky's Steam globals while
using Node's filesystem API for output cleanup. The old preset and its vulnerable
deletion dependency chain are no longer installed. The build also checks the
generated JavaScript, external globals, plugin export and embedded source map.

The combined project source is available at
https://github.com/Rayekkk/LegionGo2Companion. Its public standalone source bases
are available at https://github.com/Rayekkk/LeGoTDP,
https://github.com/Rayekkk/LeGo-Vibe-Control and
https://github.com/Rayekkk/LeGo2BrightnessFix. The Wi-Fi module also incorporates
Rayekkk's unpublished WiFi Optimizer Go 2 work; its source is included here.

The RGB page and Go 2 lighting interface were developed with reference to
HueSync 3.9.0 at commit `575fba4acd054f00629e5998eafef30169ee2005`:
https://github.com/honjow/HueSync. HueSync is BSD-3-Clause; see
`LICENSE.HUESYNC`. The joystick-ring ABI is provided by Linux's
`hid-lenovo-go` driver and is not redistributed by this archive.

The Button Remapper communicates with the InputPlumber D-Bus interface shipped
by SteamOS. InputPlumber is not bundled or modified by this archive. Its source,
profile schema and D-Bus documentation are available under GPL-3.0-or-later at
https://github.com/ShadowBlip/InputPlumber. The live target used version 0.78.0.

The gyro controls use that same external service's FilteredEvents property and Linux's
hid-lenovo-go IMU bypass attributes. Passive diagnostics independently decode the wire
fields documented by InputPlumber's Lenovo and Steam Deck HID report definitions; no
InputPlumber service or Rust source is copied into this archive. Protocol references:
https://github.com/ShadowBlip/InputPlumber/tree/v0.78.0/src/drivers/lego
https://github.com/ShadowBlip/InputPlumber/tree/v0.78.0/src/drivers/steam_deck

Battery protection uses the existing Linux power-supply charge_types ABI. The kernel
driver is external and is not redistributed. The Lenovo Long Life mode is described in:
https://github.com/torvalds/linux/commit/9ca8fc065b88b327acbfdc33454efea391639716
The Lenovo controller ABI is documented at:
https://github.com/torvalds/linux/blob/master/Documentation/ABI/testing/sysfs-driver-hid-lenovo-go
# Fan Control hardware facts and independent implementation

The Fan Control Python and TypeScript implementation is original BSD-3-Clause
code. Published hardware protocol/register facts were used from
[Rodpad/LeGo2-Fan-Control, revision de1a45e](https://github.com/Rodpad/LeGo2-Fan-Control/blob/de1a45e06c129afe7a7752224b077c3044d2d69d/main.py).
No GPL implementation was copied, adapted or transliterated into Companion.
The user separately authorized installation of the unmodified official Rodpad
plugin on their device; that installation is not a Companion dependency.
Armada Control was inspected only as a UX reference, from its public repository
and an authorized read-only copy from Odin. This editor is an independent design
and implementation; no Armada implementation was copied or executed here.
The 4E/4F two-level index protocol and C6C0/C6C2/C6C8 registers were independently
checked against Lenovo RRCN16WW EC 01.16 and bounded physical trials on 83N0 / 8ASP2.

The independently observed positive range is 1500–4800 RPM. Initial physical
trials used 100 RPM steps. The EC transport accepts integer register targets;
bounded live readback trials subsequently confirmed 1501, 1502, 1535, 1536,
3073, 4607, 4608 and 4799 register targets, including both bytes and valid
intermediate words. This confirms transport resolution, not one-RPM motor accuracy.
1500 RPM was stable after a boosted start. A 4800 RPM command reached about
4800 RPM, while a 5200 RPM command reached the same plateau. The four native
power modes were examined with baseline recovery. These observations describe
this firmware/unit's operating ceiling, **not a certified motor rating or proof
of cooling at every temperature/load**. HandheldCompanion separately reports
an observed 1500–5200 range in
[LegionGoTablet2.cs](https://github.com/Valkirie/HandheldCompanion/blob/main/HandheldCompanion/Devices/Lenovo/LegionGoTablet2.cs);
that numerical hardware fact was used as a bounded calibration candidate, not
as a manufacturer rating or copied implementation. The firmware's 10000 RPM
clamp is not a rated maximum.

Native zero override means firmware automatic. Custom zero RPM instead uses
override 256, which independently analyzed EC code maps to effective zero;
physical trials confirmed tach/effective zero and restart, then automatic
release after controller death. Positive 100–1400 RPM cannot be promised and
are skipped by the editor. Stop follows the saved curve; a positive target
restarts directly at the requested speed without a fixed spin-up boost. The old software stop-temperature clamp was
removed at the user's explicit request. This does not modify native thermal protections.

Two-byte transitions choose a bounded byte order. Boundary pairs such as
1500↔4800 require a three-write bridge through an interior high byte; every
positive-to-positive intermediate remains between 1500 and 4800. Tests cover
every pair, individual IO interruptions and process death after each target
byte. Lease withdrawal is rechecked immediately before each data write;
recovery releases the target and restores the pre-session selectors under the
same lock. A conflicting owner causes withdrawal.

The custom curve holds its first speed below its first editable temperature and
interpolates across the ordered 0–100°C domain. Only its last point is fixed:
100°C at full speed. At >=100°C the worker bypasses its ramp; the independent
backup can enforce the same maximum under the shared EC lock, with verified
ownership metadata and a marker adopted by the worker. At 85°C no software
handover occurs. High-temperature behavior is tested with injected/model samples;
the physical console is not heated to 100°C. Fault, disable, unload and suspend
still restore firmware automatic; native thermal/emergency protections remain.
Custom control does not automatically re-enable after a sensor, watchdog, thermal
or suspend fault. It requires explicit user activation after the fault clears.

Rise is up to 2000 RPM per 250ms sample; descent requires a 2°C temperature fall
and five seconds of dwell, then steps down by 300 RPM at most once per two
seconds. Startup requests the curve speed directly; there is no artificial
4000 RPM impulse or subsequent descent from it. These asymmetric parameters suppress cooling oscillation
while allowing a full-range rise within two samples. They are tested policy,
not user tuning knobs. A rolling twelve-second target history permits physical
ramp time after a higher target while still detecting a stopped running fan.
Eight curve points occupy distinct 1°C coordinates. The first seven can move,
RPM is nondecreasing between points, and point eight stays at 100°C with a speed
of at least 1500 RPM; at 100°C and above the controller runs at full speed
whatever the curve says. Both metadata and curves use the same revision check.
Validation also rejects decreasing RPM at the RPC and controller boundary. New
defaults begin at 0°C.

Settings use schema 4, the format Fan Control first shipped with (2.1.0); any
other schema is refused for review. `assign_tdp_presets` defaults to false, so
assignments remain dormant until explicitly enabled. With the option off the backend never queries the
Companion TDP preset and applies only the global curve. Changing this option
uses the same revision check as curve/unit writes, never starts an idle worker,
and applies the new selection to an already active controller. A failed active
switch disables custom control and closes its controller.

The three hardcoded starting curves are project choices, not manufacturer
profiles or claims of optimal cooling under every load. Silent uses stop only
at the cold end through 35°C and rises to 1500 RPM by 45°C; Balanced always
requests positive cooling, starting at 1500 RPM; Aggressive raises cooling
earlier. At every temperature Silent is no faster than Balanced, which is no
faster than Aggressive. All curves are monotonic with eight points and fixed
100°C/4800 RPM endpoints. Existing hysteresis and independent recovery
still apply. New settings use a fresh copy of Balanced, RPM units, assignments
off and custom control off. Catalog values are copied for RPC replies and
editor drafts. Saving never persists a template ID. The displayed template
name is derived only by exact point comparison, including after save/reopen.

A repeatable, bounded ceiling check on this 83N0/EC1.16 unit stepped through
4800, 4900, 5000, 5100 and 5200 RPM. Override and effective registers accepted
each target, but tach speed saturated near 4800. In the repeat trial, after a
20-second startup exclusion, target 4800 averaged 4794.6 RPM (4770–4813); target
5200 averaged 4796.0 RPM (4791–4802), excluding its first eight seconds. The
roughly 1 RPM difference is below observed tach variation and provides no useful
increase. The product ceiling remains 4800 RPM. These measurements establish a
tested plateau through 5200, not a manufacturer-rated maximum or evidence that
the firmware's 10000 RPM numeric clamp is physically attainable. Both trials
used isolated controllers, independent recovery and finite thermal/time limits;
settings and other hardware controls were preserved and EC automatic restored.

The tested sysfs exports k10temp `temp1_label=Tctl` and amdgpu `temp1_label=edge`,
with no Tdie channel or verified separate package-temperature channel. Linux
[k10temp documentation](https://docs.kernel.org/hwmon/k10temp.html) distinguishes
Tctl (thermal-control value) from optional Tdie. The
[amdgpu documentation](https://docs.kernel.org/gpu/amdgpu/thermal.html) describes
GPU on-die channels. EC C307 is retained as supplementary telemetry with range validation; its physical
sensor identity is unproven. An instantaneous EC/APU temperature difference is not
a freshness check and does not withdraw custom control. Display/control use max(Tctl, GPU edge), explicitly
labelled as that aggregate rather than a newly claimed physical APU sensor.

The detached EC owner and independent recovery process use persistent port/sensor
descriptors, a three-second monotonic progress/heartbeat lease, pidfd identity,
exclusive instance ownership, bounded transport locks, and a terminal stop marker
committed under the same lock as writes. Recovery never restores a prior fixed
target. No watchdog loop depends on React, Decky RPC, CDP, network, or subprocess
calls. Sleep preparation uses the system's external libsystemd through its
documented sd-bus ABI: [login1 delay inhibitors](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md),
[message processing](https://github.com/systemd/systemd/blob/main/man/sd_bus_process.xml)
and [descriptor ownership](https://github.com/systemd/systemd/blob/main/man/sd_bus_message_read.xml).
Each process obtains a separate bus connection and delay FD after fork; both
retain their inhibitor until automatic readback. Preparing sleep, login1 owner
changes, bus failure and queue overflow cause terminal recovery. No systemd
implementation or binary is bundled, and no new pip dependency is introduced.
When custom control is off there are no fan workers, inhibitors or background EC polling.
Visible QAM requests a bounded passive tachometer/sensor read. The observer holds
the same ownership lock as the detached controller, restores EC selectors and
closes raw-port/sensor FDs before releasing it. RPC cancellation retains the
mutation lock until this read completes. Active control supplies readings from
its own fresh telemetry, so an observer never competes with that owner. APU is
the maximum of k10temp/amdgpu; stale readings expire rather than displaying a
closed snapshot or a target as measured RPM. Hiding QAM stops new requests.
A functioning kernel and EC transport remain required: firmware lease and
recovery from a whole-system hang have not been established. Userspace locking
cannot exclude firmware/SMM or every non-cooperating port writer.

### Direct startup validation (2026-10-11)

Four cold trials on the validated Legion firmware confirmed real stop
(override 256, effective/tach zero for two seconds) followed by direct 1500 RPM,
without the former 4000 RPM/three-second impulse. The impulse was our software
policy, not a proven EC requirement. Raw timing and tach traces are retained in
the private startup-validation evidence. These trials support this device and
firmware, not a guarantee for every rotor, temperature or firmware revision.
The twelve-second startup/underperformance bound, lease, independent backup,
100°C maximum and automatic release remain in place; absent or insufficient
tach releases control rather than applying repeated hidden kicks.

## Desktop frontend

`desktop/native/` contains the independent C++17 / Qt Quick / QML application.
It presents the same nine modules through the existing guarded Python RPCs.
The ordinary Decky build and its `src/*.tsx` panel source are preserved.
`scripts/desktop-contract.mjs` derives the explicit RPC contract from those
panels, excluding Steam foreground-app hint setters. The UI has no EC/HID
transport and owns no hardware. No web engine or browser frontend is included.

The Linux x86-64 payload in `dist/desktop/` is the executable alone; its hash is
recorded in `native-manifest.json`. It dynamically links the system Qt 6.9 or
newer that SteamOS provides; Qt is not redistributed. Source builds use
CMake/Ninja and any Qt 6.9+ SDK; see `scripts/build-native.py` and
[docs/DESKTOP_UI.md](docs/DESKTOP_UI.md).

`desktop_bridge.py` calls the same guarded Plugin RPCs; `desktop_install.py` and
`desktop/launch.py` register and execute the native UI as the desktop user. They introduce no second
hardware controller or settings store. The design follows the Desktop analysis
recorded in the Automat (2) chat. Implementation and local/SteamOS validation
boundaries are documented in [docs/DESKTOP_UI.md](docs/DESKTOP_UI.md).

## Controller firmware updater

Both UI panels use `firmware_check`, `firmware_download`, `firmware_install`
and shared `firmware_get_status`. `controller_firmware_catalog.py` queries the
official Lenovo 8ASP2 model/component feed. `controller_firmware.py` reads the
installed versions from the in-tree `hid-lenovo-go` sysfs attributes and
authenticates original signed bytes with an independently pinned RSA-3072/PSS
key, signed component footer and JLFS CRC checks. Future release versions and
official filenames are selected dynamically; the reference snapshot is not a
release whitelist.

Installation delegates to fwupd's `legion-hid` plugin (`fwupdtool install-blob`),
the engine SteamOS ships for this controller. Companion only wraps the verified
images in fwupd's 24-byte container header. Installation is enabled after
supervised hardware reinstalls (see the qualification table in
[docs/CONTROLLER_FIRMWARE.md](docs/CONTROLLER_FIRMWARE.md)); `INSTALL_ENABLED` is
its kill switch.

Primary vendor artifacts: Lenovo LegionSpace 26.07.10.01 and original S3
controller images. JLFS/SFC format reference:
https://github.com/kagaimiq/jl-misctools (firmware/fwunpack_newfw.py and cipher.py).
fwupd container and protocol reference: https://github.com/fwupd/fwupd
(plugins/legion-hid). No reference source is included. The signed footer is not
a model/version certificate; model/target selection depends on the official TLS
feed and fresh device checks.
See [controller firmware boundaries](docs/CONTROLLER_FIRMWARE.md).
