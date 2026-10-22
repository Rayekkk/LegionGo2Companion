# Controller firmware updates

Companion checks Lenovo's controller firmware feed, downloads and verifies the
images, and installs them with fwupd's `legion-hid` plugin, the engine SteamOS
itself uses for the Legion Go 2 controllers.

Installation is enabled (`INSTALL_ENABLED = True` in `controller_firmware.py`;
`False` is the kill switch, which leaves Check and Download working and shows the
reason instead of Install).

## Source and new releases

- **Feed:** Lenovo's Legion Space service, `https://gms.service.lenovo.com/game`.
  `/api/getBaseUrls` must name that same domain, a two-letter region and the
  console's model ("Legion Go 8ASP2" on the 83N0, "Legion Go 8AHP2" on the 83N1; on
  2026-10-20 both answered with the same packages); then `/api/queryPackageInfo` is asked once per component
  (left terminal 62 `com.lenovo.legionspace.lhandle.sanyi`, right 63
  `…rhandle.sanyi`, receiver 58 `com.lenovo.legionspace.rx`) with the query version
  `v00.00.00.00`, so the answer is always Lenovo's current release, not one chosen
  from the installed version. No serial number is sent.
- **Images:** only `https://s3.service.lenovo.com/legion/cms/package/<name>.bin`.
- **New releases need no plugin update.** Versions are not pinned: any image the feed
  offers is accepted when it is signed with Lenovo's pinned RSA-3072-PSS key, carries
  the right component footer and passes the structure and CRC checks. Companion offers
  it only when its version is newer than the installed one (never a downgrade).
  A plugin update is needed only if Lenovo changes the service, the signing key, the
  package names or the image format; then Check fails with a reason instead of offering
  anything.
- The check runs when the user presses **Check for updates**; there is no background
  polling.
- The feed often answers in ~0.2 s but on 2026-10-17 a third to a half of its replies
  were HTTP 500 after 10 s. Each request has 3 s and is retried on server errors and
  timeouts within an 85 s budget for the whole check (90 s parent deadline).

## Steps

Decky and the desktop app show the same backend state and one button whose label
follows it:

| Phase | Button | RPC |
|---|---|---|
| `idle`, `up_to_date`, `installed`, `failed` | Check for updates | `firmware_check` |
| `available` (a newer version exists) | Download | `firmware_download` |
| `downloaded` | Install (opens a confirmation) | `firmware_install(token)` |
| `checking`, `downloading`, `installing`, `verifying` | disabled | none |

`firmware_get_status` returns the phase, message, the component table
(installed, available, newer, downloaded), `install_enabled`, `install_reason`
and the confirmation `token`.

Both UIs show the same red risk notice (`RISK_NOTICE` in `desktop/native/firmware.mjs`) above the
buttons until a real update to a newer version has been verified; then delete it.

1. **Check** (read-only). Installed versions come from the `hid-lenovo-go`
   sysfs attributes: root `firmware_version` (receiver), `left_handle/` and
   `right_handle/`. Gates: DMI LENOVO / 83N0 / Legion Go 8ASP2 or 83N1 / Legion Go 8AHP2, exactly one
   17EF:61EB USB device with one HID child bound to `hid-lenovo-go`, hardware
   generation 2, a valid `YYMMDD`+hex version, and two equal reads (the driver
   returns a cached value after a query timeout). Versions are read before and
   after the Lenovo feed query and must match.
2. **Download**. All three feed images are downloaded, because fwupd needs all
   three in its container even when only one device is written. Each image is
   verified once (pinned RSA-3072-PSS key, component footer, JLFS directory and
   CRC checks) and kept in memory, bound to the check that selected it. A check
   older than 15 minutes must be repeated.
3. **Install**. The confirmation lists old → new versions and sends the token,
   a hash of the check id, target components, versions and image digests.
   Power must be a connected charger or the console battery (`BAT0`) at 30% or
   more, and each pad being updated must be attached to the console with at
   least 30% battery (read from the controller's input report); a refusal here
   keeps the download. Before each pad write the attach and battery check runs
   again. Check is refused while another firmware updater runs. Companion then pauses its own
   controller writes (RGB, vibration, remap, gyro), checks power again, writes
   the container to a private temporary directory and, for each newer
   component (left, right, then receiver), re-reads the
   versions, matches the fwupd device by instance id and version, refuses while
   `fwupdtool`, `fwupdmgr` or Steam's `steamos-firmware-update` runs, and runs
   `fwupdtool install-blob <container> <device-id> --no-reboot-check --verbose`
   (stopped only after 600 s without new output or 30 minutes in total). It
   re-reads the versions up to 10 × 2 s afterwards. The exit code and the end
   of the fwupd output go to the diagnostics log.

fwupd keeps running if Companion or Decky restarts during a write. A new
Companion process that finds an updater running keeps RGB, vibration, remap and
gyro paused until it exits (at most 30 minutes; after that they stay paused
until the next restart).

A failure reports the error and returns to Check. Nothing is persisted, so there
is no lock: re-check and retry, or use Legion Space on Windows. Neither fwupd nor
Companion can recover an interrupted write; the controller keeps its bootable
image and the update can be run again.

## Container

fwupd `legion-hid` expects six little-endian u32 values, `(size, version)` for
the receiver, left and right images, followed by the three images unchanged.
The version is the hex date literal (`260422A` → `0x0260422A`). fwupd takes the
target id from each image footer (receiver 2, left 7, right 8) and writes only
the image matching the device. Built this way from LVFS sub-images, the
container is byte-identical to the LVFS payload.

One Install updates every newer component in that order; the user confirms once.

## Hardware qualification (2026-10-17)

Legion Go 2 83N0, SteamOS 3.8.28, fwupd 2.0.18, AC power. Same-version reinstalls
through the plugin's own Check → Download → Install in Decky (a console-only test
patch marked one component as newer):

| Component | Version | fwupd | Result |
|---|---|---|---|
| Right controller | 260310A | exit 0, 69.5 s | verified after 2 s; stick, buttons, triggers, gyro, touchpad OK |
| Left controller | 260310A | exit 0, 45.2 s | verified after 2 s; full function OK |
| Receiver | 260422A | exit 0, 37.1 s | USB re-enumerated, verified after 8 s; both controllers, InputPlumber profile, IMU and RGB OK |

The driver occasionally returns a garbage receiver version for one read (for example
`2606404`); the two-equal-reads rule ignores it.
