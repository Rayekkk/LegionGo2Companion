# Diagnostic logging

## Desktop log browser

In the native Desktop app open **About → View logs**; the page collects events when it
opens, and **Refresh logs** collects them again. The backend's event file is root-only, so
the app reads it through the backend (`desktop_get_logs`). The page combines
the shared backend, Desktop app and available Fan and Wi-Fi worker
events. It does not change hardware settings or require modules to be enabled.

The list opens on **Problems** (warnings and errors, including critical ones); switch to
**All events** or **Errors only** with the buttons above it. Each row has a short title
(unknown events show their humanised name), the module, and the operation, outcome and
reason when the record has them; times are shown in local time. Filter by source, module
and time (all collected events, the last 15 minutes, hour or 24 hours), or search event
names, operation IDs and error codes; **Reset filters** clears them. Select a row to inspect its
structured details. If the backend reports that it could not open its event log file, the
page says that only recent journal events are shown. Collection is manual and bounded: up to 1000
newest combined events from retained files and the last seven days of the worker
journal. Missing sources, partial records and limits are indicated explicitly.
The time filter narrows this collected snapshot; it does not fetch older history.

**Export to Desktop** saves the currently filtered snapshot as UTF-8 JSONL, using a timestamped
`LegionGoCompanion-logs-*.jsonl`
filename. The first line is a short header (`type: export_header`: time, filters, count);
the newest plugin `startup`, `runtime` and `startup_complete` records (version, model, BIOS,
SteamOS) follow even when the filter hides them, then one structured event per line.
**Copy to clipboard** puts the same report on the clipboard.
Export uses SteamOS's configured Desktop folder, never overwrites an
existing file, and reports the resulting path. If that folder is unavailable,
export reports an error rather than silently saving elsewhere. Read/export work
runs off the UI thread. Desktop logs remain viewable when the backend disconnects;
previous backend records are retained and marked as stale until refreshed.

Companion records operations, failures and recovery without changing hardware
admission, retry policy or saved settings. Normal sensor polling, fan telemetry,
USB packets and successful background reads are not a stream of log messages.

## Where to find logs

| Part | Destination | Retention |
|---|---|---|
| Decky backend and shared module operations | `/var/log/legiongo2companion/events.jsonl` (root, `0700`; files `0600`) | 1 MiB per file, current file and two rotated backups |
| Native desktop app | `$XDG_STATE_HOME/legiongo2companion/desktop-events.jsonl`, normally `~/.local/state/legiongo2companion/desktop-events.jsonl` (directory `0700`; files `0600`) | 1 MiB per file, current file and two rotated backups |
| Independent Fan owner and recovery process | system journal, identifier `legiongo2companion-fan` | Managed by the operating system |
| Independent Wi-Fi rollback process | system journal, identifier `legiongo2companion-wifi` | Managed by the operating system |
| Backend file-sink initialization failure or uninstall after its file sink was closed | journal identifier `legiongo2companion-backend`, starting with `diagnostics.file_sink_unavailable` (the failed check or error class); a one-line Decky log notice states the same reason | Managed by the operating system |

The structured file is kept out of `~/homebrew/logs`: Decky chowns that whole tree
to the desktop user each time it loads a plugin, and a root process must not
append to files another user controls. `/var/log/legiongo2companion` is created
by the backend (root, `0700`) on first start. A directory that is not a real
directory or not owned by root is refused, and events then go to the journal.
Uninstall leaves it in place, as Decky leaves its own plugin logs, so the record
of the uninstall survives; it holds at most 3 MiB and can be removed with
`sudo rm -r /var/log/legiongo2companion`. The 2.2 development builds wrote to
`~/homebrew/logs/LegionGo2Companion/diagnostics`; that folder is no longer read.

Decky's own text logs under `~/homebrew/logs/LegionGo2Companion` remain useful
for loader/import failures and messages from dependencies. The backend keeps
the newest ten of them and prunes older ones at startup.

The private Fan records live in `/run/legiongo2companion-fan/` (root only, gone
after a reboot): `last.json` is the live state of the current or last session;
`last-fault.json` appears only after a retry or fault ending and keeps the first
cause of that session with its sensor sample, until a new session fails.
Controller firmware steps, including the
exit code and the last lines of `fwupdtool` output, are ordinary `firmware`
events; they never authorize replay or prove that a physical update succeeded.

For a report, include the time of the problem, the action being performed,
the Companion and SteamOS versions, and the current event file plus its rotated
backups. If the desktop app lost its connection, include its event files too.
Independent worker records can be collected over a relevant interval, for example:

```sh
sudo journalctl --since "30 minutes ago" -o short-iso \
  -t legiongo2companion-fan -t legiongo2companion-wifi \
  -t legiongo2companion-backend
```

The journal may be volatile on some installations. File rotation and journal
retention are different; neither promises an unlimited history across reboots.

## What an event means

Backend records contain UTC time, severity, component, stable event name, process
ID and process-session ID. A public operation also has an `operation_id` shared
with its asynchronous and thread work. Its source distinguishes Decky from the
desktop app. A desktop HTTP deadline and a later completed admitted operation
retain that same operation ID; a lost response does not mean that a write was
cancelled.

Each backend start records `plugin.startup` (plugin `version`), `plugin.runtime`
(`kernel`, `python`, `architecture`, DMI `model`, `bios` and `ec` firmware
strings, SteamOS `steamos`/`steamos_build` and the `decky` loader version; none
is read from the EC) and `plugin.startup_complete` (comma-separated `ready` and
`failed` modules). Applied TDP limits are `tdp.limits_applied` with `spl`,
`sppt`, `fppt` in watts, `source` (`wmi` or `ryzenadj`) and `reason` (`game`,
`global`, `ac`, `drift`, `startup`, `rollback`; empty for an RPC, whose
`operation` names it). A saved OLED mode is `display.mode_changed` with
`previous`, `mode` and `pending` (a Gaming Mode restart is still needed).

Examples of useful boundaries include:

- RPC start, returned success/failure, rejection and caller cancellation;
- module startup, deferred recovery and cleanup;
- settings-store corruption, backup/write failures and recovery;
- Fan readiness, lost transport/lease, latched fault and release evidence;
- Wi-Fi scan/transaction/rollback stages, without network identifiers;
- EDID write intent, partial-write recovery and deferred ownership checks;
- firmware check, download, install steps per component and their failures,
  including the `fwupdtool` exit code;
- desktop connection failures and recovery, service actions, missing plugin,
  and QML warning source locations.

Fan worker events carry a `worker_id`. The desktop app also has
its own request IDs. Correlate native transport records with backend operations
using method and time; these request IDs are not shared backend operation IDs.

An exception records its class, errno when available, and bounded traceback
locations: file basename, function and line. Cause classes/errno are preserved.
Exception classes defined by this plugin (for example `InputPlumberError`,
`FirmwareError`, `CorruptSettings`) also record their message, at most 160
characters after path/address/credential redaction; their text is written by
this code. It does not serialize traceback locals, source lines, commands, the
text of library or system exceptions (which can carry SSIDs, names or packets)
or a whole RPC result. For example, a peer reset retains
`ConnectionResetError` and errno 104. User-facing error wording stays in the UI.
Asyncio error records also use fixed text and exception metadata, rather than
formatting task representations or arbitrary exception-handler context. This
does not replace the event loop's exception handler or change task cancellation.

`rpc_completed` means the call returned without a reported failure. It is not an
independent hardware measurement. Hardware restoration and uncertain firmware
outcomes retain their existing verification requirements. Healthy status polling
cannot turn an uncertain update into success.

## Noise, privacy and logging failures

Repeated background failures are coalesced, normally with a 60-second summary.
Changes and recovery are recorded promptly. Store, attribute and stage identify
separate failures, so one settings file does not suppress another file's error.
The remembered keys and queues are bounded. Success is not logged per module,
attribute or poll: a healthy start is `startup`, `runtime` and `startup_complete`,
and monitored states declared with `transition(..., initial=False)` (RPC health,
settings backups, managed profiles, RGB/vibration reconcile, TDP loops, native
display detection) stay silent until they first fail, then record the failure
and the recovery. A cancelled backlight wait no longer closes its descriptor
under the waiting thread, so stopping the display module logs no `EBADF`.

New structured events use selected scalar fields. They do not record RPC
arguments, whole settings or profiles, game names/history, session capabilities,
credentials, SSIDs/BSSIDs, controller serials or packet contents. Legacy text has
additional redaction, but code must not rely on regular expressions to make
arbitrary input safe: log fixed codes and exception metadata instead.

Backend file writes run in a dedicated bounded queue. An unavailable file,
permission error, full disk, full queue, missing journal or logging error cannot
reject a control operation, alter its result or prevent recovery. Queue pressure
can lose diagnostic records; a later write reports dropped records. Persistent
faults receive periodic summaries, including after a temporary sink failure.
Flush on orderly shutdown is bounded. Power loss, process termination or a hung
kernel can lose recent records, and absence of a record does not prove absence
of a fault.

Independent processes use nonblocking, bounded native journal datagrams and do
not wait for Decky, UI, a pipe reader or network access. This follows the
[systemd native journal protocol](https://systemd.io/JOURNAL_NATIVE_PROTOCOL/).
The existing recovery files remain useful when journal delivery is unavailable.

## Adding diagnostics

Use `diagnostics.event(component, action, ...)` for finite operations and
`diagnostics.transition(component, action, state, ...)` for monitored states.
Pass actual exceptions as `error=exc`; do not pass raw stderr, a failed result's
free-form error, arguments or settings as diagnostic text. Use fixed operation,
stage and outcome codes plus the allowed scalar context. The allowed context
keys are one list, `diagnostics._FIELDS`, shared by the writer and the reader; a
key missing from it is dropped silently, so add new keys there. A healthy state
that would otherwise be recorded at every start takes `initial=False`. A logging
change must not add a hardware read, write, retry, reconnect or admission condition.

Tests cover truthful outcomes, privacy, shared operation context, repeat/recovery
behavior, bounded rotation/queues, full/unavailable sinks, Unix ownership checks,
late worker failure after cancellation, and preservation of the existing control
results. Linux fixture tests and live passive checks are recorded separately from
physical hardware/firmware update evidence.

Fan guardian retry and fault endings retain the first cause of the session in the
private `/run/legiongo2companion-fan/last-fault.json`: reason, stage, outcome and the
last numeric sensor/EC sample (CPU Tctl, GPU edge, EC temperature and telemetry,
acquisition times and validation branch). Every session ending is also logged as
`fan.worker_closed` (owner) or `fan.worker_recovery` (backup) with `outcome`
`pause`, `retry` or `fault`; the plugin logs `fan.session_ended`, `fan.control_resumed`
and `fan.control_fault`. No raw sensor text is recorded; the numeric sample is
only in `last-fault.json`, not in these events.

An unreadable unrelated plugin manifest emits `plugin.sibling_manifest_unreadable`
with its sibling name and bounded error metadata. It is a local inspection warning,
not proof that the plugin owns hardware. Known predecessor identities still trigger
the global conflict guard.
