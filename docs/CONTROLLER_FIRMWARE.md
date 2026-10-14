# Controller firmware: local experimental implementation

Decky and the native desktop application expose one **Check updates** button.
There are no component selectors, file pickers, directory controls or separate
image-validation actions. Both call `firmware_check_updates` and observe the
same backend job; closing either frontend does not start a second job.

The backend checks the official Lenovo 8ASP2 feed, compares all controller
versions and selects only newer components. Equal or newer installed versions
are never downgraded. All needed images must authenticate before one independent
installer can receive the complete plan. No partial plan is installed when any
image is unsupported or invalid. An unverified installation outcome blocks retry.

**Installation is enabled for the authorized experimental hardware trial.** The
complete Linux installer uses the build's `OTA_ENABLED=True` gate. Neither UI
can change that gate. A successful catalog check
or image validation is never reported as successful installation. No firmware
has been flashed in the hardware qualification rounds.

The live comparison uses HID-driver-reported versions. Reading these sysfs
attributes issues version-query USB output and can return driver-cached values
after a timeout. The result is therefore worded as no newer firmware listed for
the reported versions. Installation preflight and final success require fresh
queries on the exclusively owned USB channel, not these sysfs values. Receiver
firmware maps to the root MCU/query target1; the TX dongle/target2 is separate.

New versions and filenames are selected dynamically from Lenovo's live 8ASP2
model/component feed. No per-release URL, version, length or SHA whitelist gates
the update path. The dated reference releases remain offline test fixtures, not
the allowed set of future updates. No network request runs at startup or polling.
The older LVFS bundle is not used as a substitute.

The official feed uses fixed HTTPS paths, region bootstrap, no redirects/proxies,
strict component/version-code checks and a bounded isolated network helper.
`versionCode` selects the machine version; the presentation-only `versionName`
does not authorize or reject an image. Component, signature and transfer checks
remain mandatory. Returned
credentials and serial-number fields are neither used nor forwarded.

Preparation uses only Lenovo's official HTTPS package host, with redirects and
proxies disabled, bounded size and time, signed component footer,
RSA-3072/PSS SHA-256 (MGF1 SHA-256, salt length 32), and decoded JLFS header and
payload CRC checks. The trusted public key is pinned independently of the
incoming image. Exact SHA-256 and size are captured after authentication and
frozen with the release version/URL in the private update plan. They are checked
again during staging and worker admission, not compared to a static release
list. All components authenticate before any controller is acquired.
OpenSSL is invoked directly with fixed arguments; absence or
failure disables validation. Downloading runs in an isolated Python helper;
the parent enforces a 35-second wall deadline and reaps the helper on timeout or
cancellation, including stalled DNS/HTTP/TLS parsing. This helper never opens
controllers. Original bytes, including the footer and trailing
384-byte signature, are retained unchanged. Decoding is only a format check.

The reviewed signed format has no independently established embedded model or
release-version certificate. These identities come from the TLS model-specific
component feed, fresh device/model/generation-2 preflight, signed component
footer and post-update version queries. A signature alone cannot select a model.
Key rotation, a different signed format/hardware generation or files outside the
existing 1 MiB stream envelope fail before writing; that bound is a protocol/
staging policy, not a manufacturer-rated flash-capacity claim.

One **Check updates** activation handles all newer components sequentially and
skips already-current versions. The frozen plan survives frontend/backend loss;
uncertain writes are never automatically repeated. Normal future releases in
this verified format do not require a Companion update. Live erase/write/reboot
qualification still requires an actual newer release offered to the device.

## Implementation threshold

The user-approved threshold is safety at least equivalent to the official
LegionSpace updater. We do not require an additional guarantee against every
MCU power-loss failure before beginning implementation. Unknown vendor ROM or
bank-commit behavior remains an inherited risk, not an established guarantee.

Before enabling device transfer, the Linux implementation must establish exact
device identity, packet sizes/layout, fresh attachment and power gates,
component/version compatibility, exclusive transport ownership, ACK/error and
timeout handling, suspend inhibition and installed-version confirmation after
reconnection. A transmitted progress value of 100% is not success. Closing HID
or repeating a failed transfer is not a demonstrated rollback. The worker must
outlive Decky/UI failure once a write has begun. Current per-RPC locks are not a
complete cross-process flashing lease.

The implementation scope is the controller updater. One independent worker,
a sleep inhibitor, exclusive interface ownership, vendor protocol/error handling
and driver/input restoration are the intended production path. The additional
guardian/rescue and qualification-planner experiments below are retained research
evidence, not required production layers or a stronger-than-LegionSpace safety
threshold. The pure qualification planner is excluded from the plugin payload.

The pure packet codec follows the vendor's 64-byte `05 00 c0` reports, one-based
32-byte blocks, little-endian length/index/CRC fields and byte-count ACK
boundaries. Query targets 3/4 are distinct from Go 2 OTA targets 7/8. It rejects
short/wrong replies and does not equate a completion event to installed-version
confirmation. It performs no I/O and is tested only against offline vectors.
Version comparison currently accepts only the observed YYMMDD plus hexadecimal
revision representation with a valid calendar date. Unknown formats fail
closed; this is a compatibility policy rather than a universal vendor schema.

The local transfer core now schedules this protocol as one nonblocking session.
It authenticates the original image again, separates sent and acknowledged
bytes, requires explicit adapter time policies, and never retries an
uncertain operation. Vendor reply limits are 500/1000/500/500/4000 ms for
start/metadata/boundary/block/finish, separate from transport write limits.
The vendor's post-finish completion wait is five seconds after the finish ACK;
overall transfer and reconnect bounds remain policies requiring validation.
A matching early completion can be retained while waiting for finish ACK, but
cannot replace that ACK. Completion alone cannot produce success: a fresh,
request-correlated identity/version observation is required. The local token
does not prove freshness on the wire; acquisition is the adapter's responsibility.

A separate POSIX journal stores bounded, frozen-image transaction records using
a private owner directory, a cooperating-worker lock, anchored no-symlink file
operations and fsync/atomic replacement. Durable intent precedes the first
packet handed to the adapter. Interrupted records are display-only failures or
uncertain results, never instructions to resume or replay. The lock does not
exclude unrelated HID writers. This journal and the transfer core are packaged
but are not wired to a flash worker or either frontend's RPCs. Windows tests
verify state transitions and serialization; real POSIX filesystem protections
also passed the 2026-10-13 SteamOS diagnostic run. They do not establish
suspend inhibition, worker survival after Decky removal, transport ownership or
controller recovery after a power interruption.

An internal `controller_firmware_worker.py` now drives one session with injected
transport, journal and inhibitor capabilities. It authenticates before acquiring
resources, uses a one-shot authorization handoff rather than a frontend
heartbeat, checks monotonic deadlines and ownership around operations, and
reports cleanup failures separately. Empty non-waiting adapters are rejected
instead of busy-looping. Exceptions after possible output remain uncertain;
there is no automatic restart, replay or device rollback. The installed worker
entry point refuses device operations, and no flash RPC calls this engine.

`controller_firmware_process.py` captures bounded immutable source/image bytes
and a digest manifest, so a future worker need not read from the plugin's
directory after launch. It builds a fixed, non-shell system-service plan with
isolated system Python, no restart and a finite runtime. An internal one-shot
handoff records durable launch intent before submission; an ambiguous submit
never triggers automatic resubmission. Service observations are bounded,
identity-checked and display-only. Jobs remain available for inspection even
if the plugin disappears; no cleanup API deletes an active or unknown job.
None of these facilities is called by a plugin RPC or startup hook.

`controller_firmware_stage.py` can provision only its fixed root-owned private
directories under `/var/lib`, publish an authenticated capture with anchored
no-symlink operations, private files, a publication lock and durable rename,
and verify it before importing worker modules. The trusted launcher passes a
manifest digest separately; hashing an attacker-provided manifest would not
authenticate arbitrary code. A hostile root and storage/kernel failures are
outside this filesystem boundary. The installed isolated CLI still refuses all
device operations, even after successful staging verification.

`controller_firmware_sleep.py` implements a worker-owned login1 **block**
inhibitor for sleep and shutdown, retained as a non-inheritable FD. It binds
calls to a unique logind owner, rejects already preparing operations, bounds
method calls by the acquisition deadline, and latches loss of the connection,
owner or inhibitor. It does not request sleep or shutdown. The lock cannot
prevent forced administrator actions, power loss or a kernel failure. This
binding uses the upstream [login1 API](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml).

Windows tests cover capture validation and fault injection into service/bus
interfaces. Anchored filesystem tests run only on POSIX. Real systemd placement
and inhibitor acquisition/loss were checked on SteamOS on 2026-10-13. Service
survival after actual Decky removal and a reviewed HID adapter remain unverified;
they are required before enabling it.
The service model follows upstream
[systemd-run documentation](https://github.com/systemd/systemd/blob/main/man/systemd-run.xml).
An ordinary detached child can remain inside Decky's cgroup and is insufficient.

An offline subprocess experiment runs the authenticated worker against a fake
controller, removes a disposable producer directory and terminates only its
own disposable broker. The child completes using its retained files. This proves
that local model, not survival of a SteamOS service restart, an actual Decky
uninstall, HID reply freshness, suspend protection or controller recovery.

The 2026-10-13 SteamOS diagnostic round passed 107 native Linux tests without
skips, exercised real login1 inhibitors and an isolated finite system service,
and verified twelve fresh kernel-query version responses before a normal
user-authorized reboot and twelve afterward. The Lenovo PL catalog became
reachable after the user removed an AdGuard DNS block; its L/R 260310A and
receiver 260422A offerings matched those observations. Three official images
were downloaded and validated on SteamOS. Transfer tests still use fake
transports; no controller firmware was flashed.

Focused static analysis of the installed kernel module, matching the reviewed
SteamOS commit c8730d37f9c6458cc1100cd9149e9d7e372bf52a, confirms that its
`hid_go_raw_event` rejects OTA C0 replies before hidraw. A0 handling also does
not match the reviewed OTA completion reports. Ordinary version reads work;
changing only the output function cannot fix this receive path. libusb 1.0.30
and the interface 02 OUT 03 / IN 83 endpoint pair are available on the console,
but an exclusive claim, driver handoff and restoration of InputPlumber are not
yet qualified. No driver detach or OTA command was used to establish this.

`module_runtime` now provides a process-local controller-maintenance gate for
controller/gyro, remap, RGB and vibration. It closes new admission before
draining actual threads, including threads whose callers were cancelled.
Controller work now uses that registry, and the internal coordinator joins
its separate read-only capture thread before confirming quiescence. Queued
RPCs recheck the gate; automatic reconcile/hotplug work is paused; saved module
choices stay unchanged. Both panels can report maintenance separately from
module disablement. Drain timeout, cancellation or failed observer shutdown
keeps admission closed. Only the same internal owner with an explicit
restoration confirmation can reopen it. Admitted affected RPCs are retired
before readiness, including sleeping retry continuations. Multi-step operations
retain their original generation so a stale background retry cannot adopt the
post-maintenance generation.

A private restoration lane now lets the same quiescent coordinator restore
retained backend intent after independently establishing USB restoration.
Ordinary public RPCs remain blocked even if invoked from that lane. Monitors
do not inherit its admission; every entry has a distinct revocable token so
escaped continuations cannot borrow a later restoration. Cancelled restoration
threads remain registered until they really finish. The lane has no activation
hook and its boolean attestation does not itself establish hardware recovery.

`controller_firmware_handoff.py` is a pure, one-shot qualification planner.
It requires durable intent before returning an action, maintains absolute
deadlines, binds observations to one boot/job/baseline and orders owner death,
driver/consumer/Companion restoration and final evidence before fence release.
Uncertain actions are not replayed; failed recovery retains the closed fence.
Profiles allow no application output or one version query to target 1â€“4; no
firmware writes exist in this planner. Observation facts must be obtained by
a future reviewed adapter: injected flags and hashes are not USB ownership,
input-function or restoration proof. Nothing starts this planner from either UI.

Four additional SteamOS experiments exercised independent transient guardian
and rescue services with synthetic binding files: normal exit, own probe exit,
finite lease timeout and own guardian exit. Rescue restored the synthetic state
only after the primary service's group was empty. Production service instances,
USB driver bindings and settings stayed unchanged. This proves the tested Linux
process arrangement, not a driver detach/rebind or a firmware recovery path.

This internal coordinator has no RPC, startup activation, USB claim or flash
launch. It is not an interprocess lock, durable maintenance claim or substitute
for excluding InputPlumber/other consumers. `controller_maintenance.py` adds a
root-private cooperating-process fence under `/run/legiongo2companion`: a marker
survives its owner's process death and keeps affected modules paused in a fresh
backend. Malformed/untrusted markers fail closed; startup neither provisions
nor clears the fence. The original owner can clear its exact inode/token only
with explicit restoration attestation. That attestation is not itself hardware
proof. The marker writer is internal and is not connected to an RPC or handoff.
At this historical stage an independent worker and a bounded non-flashing
handoff test were still required. Subsequent qualification below established
the scoped USB handoff/restoration; production installer wiring remains pending. Uninstall, process death and power-loss recovery are not established
by these local concurrency tests. The installed CLI still refuses flashing.

`controller_firmware_usb_linux.py` adds a dormant, interface-02-only ownership
adapter with distinct probe and recovery roles. Its passive observer pins the
current DMI/BIOS/EC, exact kernel/module, descriptors, attachment and other
interface bindings. The reviewed libusb build is 1.0.30.12037. Operations are
one-shot and check ownership before and after; uncertain actions cannot be
retried. Recovery cannot claim or query. No reset, configuration change,
alternate setting, clear-halt or OTA entry point is exposed. Permission
callbacks require an authenticated worker coordinator and independent cleanup;
they do not supply those proofs. An additional guardian framework is not required. This adapter is not called by startup, a CLI
or either frontend. Fake transition tests are not real USB qualification.

Independent review found and reproduced expired restoration-token admission
after resume and late claim completion after a terminal clock failure. Both
now fail closed, with separate regression cases. A failed final journal commit
also remains an uncertain result instead of a closed successful job. Earlier
failed review evidence is retained alongside subsequent results.

`controller_firmware_usb.py` contains passive libusb enumeration and an internal
version-only interrupt channel for verified interface 02 OUT 03 / IN 83. It
cannot open/claim/detach/reset a controller or emit an OTA packet. Its channel
requires an already qualified handle/lease, drains a bounded stale receive queue,
accepts only version-query targets 1â€“4, and never retries uncertain output.
Ordinary gamepad input on IN 83 is not an updater reply or proof of an empty
FIFO. A bounded drain accepts only ordinary input before output and rejects
observed control replies. Matching replies do not provide a wire nonce. Tests inject
partial transfers, ownership/deadline loss, wrong targets and concurrent queries.
On 2026-10-13, libusb context/list/descriptor enumeration and 35 isolated Linux
tests passed without changing installed payload, settings, services or drivers.
No actual interrupt query or USB claim was performed in that round.
The binding follows the upstream [libusb device API](https://libusb.sourceforge.io/api-1.0/group__libusb__dev.html)
and [synchronous I/O API](https://libusb.sourceforge.io/api-1.0/group__libusb__syncio.html).

### Subsequent non-flashing USB qualification, 2026-10-13

Bounded diagnostic workers outside the installed plugin subsequently exercised
interface 02 on the exact recorded Legion Go 2/kernel/module combination, in
Desktop Mode with Steam closed. The ownership trial opened, detached, claimed,
released and rebound the command interface. Independent readback confirmed the
original InputPlumber profile, target types, filters and controller/RGB settings.
Ordinary HID clients after a successful rebind were distinguished from raw USB
ownership; no process-specific allowlist or forced shared-process termination
was used.

A single fixed left-controller version query returned a matching 64-byte
`04 00 02 04 03` response containing `260310A` in about 157 ms. Earlier attempts
failed safely: requiring silence on the whole endpoint rejected its continuous
gamepad stream, and expensive per-report ownership scans exhausted the receive
deadline. The successful diagnostic kept full checks at operation boundaries
and used inexpensive identity/fence checks between reports. The local version
channel now handles ordinary input and recomputes its timeout after lease
checks. These source changes do not enable an OTA transport or flash RPC.

Two deliberate fault trials with zero updater OTA packets also restored the command driver and
configuration: death of the owned USB probe followed by a fresh recovery process,
and death of its parent after the probe had closed, followed by the owned unit's
separate `ExecStopPost` recovery. The latter bypassed the parent's Python cleanup.
Both trials confirmed removal of the maintenance claim and preservation of the
installed payload/settings. Their journals and independent review remain in
`.local-validation/LegionGo2Companion/2026-10-13-controller-usb-handoff`.

These results cover USB handoff and restoration before firmware writing. They
do not establish recovery during an OTA write, parent death while a native
transfer remains active, power loss, kernel failure, or a post-flash version
check. Matching a reply is request correlation, not a nonce-based freshness
attestation. Physical buttons were not verified; the user confirmed that the
touchpad also did not work before these tests while Steam was closed. MagicBlack
was left off. Brightness changes observed between earlier snapshots were
recorded rather than overwritten. No firmware was flashed in that historical qualification round. The current
experimental installer is enabled as described above.

## Evidence and attribution

Protocol and signatures were investigated from Lenovo LegionSpace
26.07.10.01 / native binaries 1.4.4.31 and original Lenovo controller BINs.
Independent OpenSSL checks verified nine original current/historical images;
modified images and unrelated signing keys were rejected. This is offline
evidence, not a flash or recovery test on the console.

JLFS/SFC format facts were cross-checked against kagaimiq/jl-misctools
(`firmware/fwunpack_newfw.py`, `cipher.py`). This implementation is independently
written. fwupd legion-hid source is a transport comparison source, not a claim
that its older payloads are the latest firmware. Research reports are retained
outside this repository in `.local-validation` and `LegionSpace-RE`.


## Minimal OTA adapter, 2026-10-13

`controller_firmware_ota.py` implements bounded C0/A0 I/O on an already exclusively
owned interface02 handle. Ordinary continuous input is demultiplexed; early
completion is retained by the transaction core without replacing finish ACK.
Short/uncertain output is never replayed. Post-update observations are delegated
to the worker's fresh USB observer. The adapter does not acquire a device at startup. The current authorized
experimental backend invokes it only through the independent installer.

An explicit `allowed_boundaries=None` uses the vendor's negotiated positive
signed int32 ACK boundary (1..0x7fffffff). No additional guessed bound or
alignment is imposed. Explicit restricted sets remain available for tests.
Host deadlines bound progress even for very large or unaligned boundaries.
Simulated adapter tests do not establish live OTA ACK, flash/reconnection or
power-loss recovery.

`controller_firmware_install.py` connects the complete plan to one independent
systemd worker. Its private stage retains all authenticated images and worker
code outside the plugin directory. The worker pauses Companion controller
operations, saves actual input settings, stops InputPlumber, checks exclusive
interface02 ownership and reads versions on the owned USB wire. Each component
requires current attachment and console power before its first C0 command.
Completion requires A0, finish ACK and a new version-query reply. Expected USB
address changes are matched to the qualified physical port, not the old address.
The worker restores the HID driver, input profile, filters, targets and controller
attributes before success. ExecStopPost restores input after worker exit without
resending firmware commands. Unresolved durable jobs block another installation.

This wiring is new local code and is **not live OTA qualification**. The tested
earlier USB handoff probes do not prove this coordinator during firmware writing.
Interrupted firmware outcome remains uncertain; driver restoration does not
prove recovery of a partially written image. The current experimental build
enables the explicitly authorized hardware trial, without claiming completed
physical OTA qualification. No additional persistent guardian was added.

## Final audit corrections (2026-10-14)

Both panels require a recognized, fresh firmware status before admission and
explicitly invalidate old status after a read or mutation error. Responses include
request latency in their eight-second freshness window; unknown phases cannot
enable the button. Neither interface automatically replays a failed mutation.
Preflight timestamps cover the earliest generation/version/attachment queries,
so delayed power reads cannot refresh old observations. Optional malformed
status/journal evidence no longer prevents input restoration from the protected
critical recovery snapshot; the outcome stays uncertain and evidence faults are
retained. ACK boundaries follow LegionSpace's signed positive int32 gate
(1..0x7fffffff); the high-bit half of uint32 is rejected before block streaming.

Top-level null responses also disable the native button instead of preserving
an earlier ready binding. When a conflict pauses hardware modules, both panels
can still show the independent update outcome through read-only status. Their
update action remains disabled. Verified failures before any write, with complete
input restoration and unchanged versions, permit a new explicit check; partial
or uncertain writes remain blocked. Backend unloading does not stop an admitted
independent worker or skip cleanup of unrelated module observers.
