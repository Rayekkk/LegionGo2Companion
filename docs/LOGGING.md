# Diagnostic logging

## Desktop log browser

Open **Logs** in the native app's sidebar, then **Refresh logs**. The page combines
the shared backend, Desktop app and available Fan, Wi-Fi and firmware worker
events. It does not change hardware settings or require modules to be enabled.

Filter by severity, source, module and time, or search event names, operation IDs
and error codes. Warnings and errors includes critical errors. Select a row to
inspect its structured details. Collection is manual and bounded: up to 1000
newest combined events from retained files and the last seven days of the worker
journal. Missing sources, partial records and limits are indicated explicitly.
The time filter narrows this collected snapshot; it does not fetch older history.

**Export to Desktop** saves the currently filtered snapshot as UTF-8 JSONL, one
structured event per line, using a timestamped `LegionGoCompanion-logs-*.jsonl`
filename. The app uses SteamOS's configured Desktop folder, never overwrites an
existing file, and reports the resulting path. If that folder is unavailable,
export reports an error rather than silently saving elsewhere. Read/export work
runs off the UI thread. Desktop logs remain viewable when the backend disconnects;
previous backend records are retained and marked as stale until refreshed.

Companion records operations, failures and recovery without changing hardware
admission, retry policy or saved settings. Normal sensor polling, fan heartbeats,
USB packets and successful background reads are not a stream of log messages.

## Where to find logs

| Part | Destination | Retention |
|---|---|---|
| Decky backend and shared module operations | `DECKY_PLUGIN_LOG_DIR/diagnostics/events.jsonl` | 1 MiB per file, current file and two rotated backups |
| Native desktop app | `$XDG_STATE_HOME/legiongo2companion/desktop-events.jsonl`, normally `~/.local/state/legiongo2companion/desktop-events.jsonl` | 1 MiB per file, current file and two rotated backups |
| Independent Fan owner and recovery process | system journal, identifier `legiongo2companion-fan` | Managed by the operating system |
| Independent Wi-Fi rollback process | system journal, identifier `legiongo2companion-wifi` | Managed by the operating system |
| Controller firmware worker/recovery | system journal, identifier `legiongo2companion-firmware` | Managed by the operating system |
| Backend file-sink initialization failure or uninstall after its file sink was closed | Decky log notice; structured fallback uses journal identifier `legiongo2companion-backend` | Managed by Decky / the operating system |

Decky normally places the plugin log directory under
`/home/deck/homebrew/logs/LegionGo2Companion`. Its existing log files remain useful
for loader/import failures and messages from dependencies. The new structured
files do not replace Decky's own loader logs or prune historical records.

The existing private Fan record
`/run/legiongo2companion-fan/last-fault.json` is retained independently of event
logging. It records the last fault and recovery observation, survives a clean
fan session, and disappears on reboot. Controller firmware status and transaction
journals remain separate recovery evidence; event logs never authorize replay or
prove that a physical update succeeded.

For a report, include the time of the problem, the action being performed,
the Companion and SteamOS versions, and the current event file plus its rotated
backups. If the desktop app lost its connection, include its event files too.
Independent worker records can be collected over a relevant interval, for example:

```sh
sudo journalctl --since "30 minutes ago" -o short-iso \
  -t legiongo2companion-fan -t legiongo2companion-wifi \
  -t legiongo2companion-firmware -t legiongo2companion-backend
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

Examples of useful boundaries include:

- RPC start, returned success/failure, rejection and caller cancellation;
- module startup, deferred recovery and cleanup;
- settings-store corruption, backup/write failures and recovery;
- Fan readiness, lost transport/lease, latched fault and release evidence;
- Wi-Fi scan/transaction/rollback stages, without network identifiers;
- EDID write intent, partial-write recovery and deferred ownership checks;
- firmware catalog, authenticated image, admission, worker/component progress,
  restoration and terminal status;
- desktop connection failures and recovery, service actions, missing plugin,
  and QML warning source locations.

Firmware check jobs and independent workers have different IDs. Parent events
record both `job_id` and `worker_id` when available, so their histories can be
joined without logging the update plan or USB payload. The desktop app also has
its own request IDs. Correlate native transport records with backend operations
using method and time; these request IDs are not shared backend operation IDs.

An exception records its class, errno when available, and bounded traceback
locations: file basename, function and line. Cause classes/errno are preserved.
It does not serialize traceback locals, source lines, commands, arbitrary
exception text or a whole RPC result. For example, a peer reset retains
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
The remembered keys and queues are bounded. Debug-level per-attribute success
messages are disabled in the normal event stream.

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
stage and outcome codes plus the allowed scalar context. A logging change must
not add a hardware read, write, retry, reconnect or admission condition.

Tests cover truthful outcomes, privacy, shared operation context, repeat/recovery
behavior, bounded rotation/queues, full/unavailable sinks, Unix ownership checks,
late worker failure after cancellation, and preservation of the existing control
results. Linux fixture tests and live passive checks are recorded separately from
physical hardware/firmware update evidence.

Fan guardian faults retain their first rejected sensor snapshot in the private
`/run/legiongo2companion-fan/last-fault.json`: CPU Tctl, GPU edge, EC temperature,
monotonic acquisition times, duration and the specific validation branch. Later
automatic-mode confirmation updates recovery status without replacing that sample.
A bounded numeric `fan.fault_sample` event is also available in desktop logs and
exports. Missing values are omitted; no raw sensor text is recorded.

An unreadable unrelated plugin manifest emits `plugin.sibling_manifest_unreadable`
with its sibling name and bounded error metadata. It is a local inspection warning,
not proof that the plugin owns hardware. Known predecessor identities still trigger
the global conflict guard.
