# Optimization review — 2026-09-28

This work follows the local dead-code cleanup documented in
`CLEANUP_REVIEW.md`. The starting point is the cleaned working tree based on
`0ea73bca81ef2239a4fa6d2bb2f8e478e896015d`, not an unmodified 1.0.4 checkout.
Earlier cleanup changes are preserved. The release version is unchanged.

## Scope and decisions

Reviewed all first-party backend modules, frontend pages, lifecycle and settings
helpers, update/download paths, and build/package configuration. Changes target
repeated work with reproducible evidence. Hardware verification, recovery,
transaction ordering, polling intervals, dependency versions, and public RPC
names remain unchanged.

| Area | Before | After and preserved behavior |
| --- | --- | --- |
| TDP page initialization, no game running | Seven page-initialization RPCs, including repeated settings and charger reads | Five RPCs. A later menu opening still refreshes charger state; game exit still restores global settings. |
| Vibration page initialization | Five RPCs, including two driver-status reads | Four RPCs. Reopening still refreshes the driver, and newer hotplug events take precedence over an earlier response. |
| CPU controls during repeated menu openings | Three reopenings while the first read was pending created four concurrent requests | One pending read plus one fresh follow-up, two sequential requests in that scenario. The follow-up uses the latest game/AC context; old replies cannot populate the new editor. |
| Readiness retries | A queued retry could make another RPC after unmount | Timer cleanup plus an active check prevent calls after unmount. |
| Unchanged conflict scans | Ten scans also caused twenty version reads and twenty display-session probes when a saved display session was present | Ten scans, zero auxiliary version/session reads. A changed guard still publishes a complete current status, and the restart latch remains active. |
| Settings commits | The same snapshot was validated/JSON-encoded twice and deep-copied again for rollback storage | One encoding and one snapshot copy per commit. Identical bytes are written to primary and backup; both atomic writes and their durability checks remain. |
| Wi-Fi status | Two reads of the same settings file | One shared snapshot for status, ownership information, and support tier. |
| Controller diagnostics | `setdefault` constructed a new `deque` on every valid report and snapshot, even when the buffer already existed | One buffer per stream. The bounded 4096-entry history, report counts, invalid-report counts, and rate expiration remain unchanged. |
| Remapper discovery | Both Name and Capabilities were queried for unrelated controllers | Capabilities is queried only after Name matches. This saves one `busctl` process per unrelated candidate and retains discovery after property-read failures. |

These are operation-count and concurrency improvements, not measured claims
about SteamOS CPU usage, battery life, frame rate, or hardware response time.

## Evidence and regression coverage

- The frontend lifecycle harness checks initialization RPC counts, later menu
  openings, hidden initialization, newer hardware events during slow initial
  requests, and cancelled readiness timers. CPU tests also cover repeated
  openings, context changes during an in-flight read, and suppression of queued
  refreshes after hiding or unmounting the editor.
- Ten unchanged guard checks were measured before and after: conflict scans
  stayed at 10; version/session reads each fell from 20 to 0. Tests also cover
  conflict appearance/removal and the restart latch.
- A settings commit with 512 synthetic profiles changed from two JSON encodes
  to one, while retaining two write calls. Storage tests cover nested snapshot
  isolation, serialization/primary/backup failures, and crash recovery. An
  independent comparison against the pre-optimization code produced identical
  results for rollback and recovery scenarios.
- The Wi-Fi status regression checks one settings-file read and agreement
  between the support tier and the settings snapshot returned to the UI.
- The diagnostics regression processes 8202 synthetic reads: 4100 valid and one
  invalid report per stream. Exactly two buffers are allocated, counts remain
  intact, history stays bounded, and rates expire correctly.
- Remapper tests check the precise property requests for unrelated controllers,
  disconnected candidates, and the matching Go 2 device.

## Areas left unchanged

TDP enforcement, vibration hotplug/drift handling, display notifications and
gamescope caching, RGB recovery, battery/IMU transactions, and process watching
already have bounded or event-driven paths. Further caching or fewer readbacks
would require device measurements and could hide state changes. Small duplicate
display/RGB reads were left in place rather than changing freshness assumptions.
Wi-Fi band transactions already run outside the main event loop; their worker,
locking, cancellation, and rollback paths were retained.

Download validation remains bounded and streaming; TLS contexts are already
reused where needed. Build/package inputs and the unmodified vendored pyudev
library were retained. No dependency upgrade or UI redesign was included.

## Validation and limits

Local validation uses Windows, Python 3.10.9, and Node.js 24.18.0. The complete
backend suite passes: 386 tests collected, 368 passed, 18 skipped. The skipped
count is unchanged from the cleaned baseline and includes platform-specific
checks that this Windows host cannot execute.

All nine frontend regression suites, strict unused-code/type checks, build,
package validation, archive verification, and `git diff --check` passed.
Hardware calls in tests are mocked; no installation or device validation was
performed for these optimizations, and the Linux CI matrix was not run locally.
This review did not create a release.
