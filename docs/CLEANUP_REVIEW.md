# Dead-code review — 2026-09-28

This focused review used `main` at `0ea73bca81ef2239a4fa6d2bb2f8e478e896015d`
(version 1.0.4) as its baseline. The cleanup does not change the release version
or publish a release.

## Review method

Reviewed the Decky entry point, all first-party Python modules and frontend pages,
tests, build inputs, package contents, and vendored files. Deletion decisions used
repository-wide references, Python AST inspection, TypeScript unused-code checks,
and tracing from RPC, lifecycle, CLI, and build entry points. Low reference counts
alone were not treated as proof of dead code. An independent review checked the
backend diff, including startup and Wi-Fi recovery paths.

## Removed

| Area | Evidence and change |
| --- | --- |
| Display and vibration updater helpers | Removed `display_updater.py` and `vibration_updater.py`, their instances, imports, and TLS warm-up calls. Neither module performed network requests. Their only remaining effects were duplicate startup version logs and unused private TLS contexts. Companion still logs its version in `main.py`. |
| Display backend | Removed `_mode_script_name` and `_count_gamescope_atoms`; neither had a caller. |
| Component version methods | Removed the TDP, display, and vibration backend `get_version` methods. The actual public RPC responses are implemented directly in `main.py`. |
| Vibration profiles | Removed the old bulk `set_game_profiles` method, which was neither exposed by Companion nor called internally. Active profile editing and migration remain. |
| Wi-Fi backend | Removed 19 unreachable definitions: the unused lock alias, deprecated feature stubs and their response helper, old diagnostic export methods, `reapply_volatile`, `reapply_all`, `set_auto_fix`, and the unused asynchronous capabilities adapter. The synchronous capabilities probe remains active. |
| Frontend | Removed six unused imports, two unused standalone icons, the unused TDP `valueTag` style, and the write-only `Overview.standalonePlugins` copy. The conflict screen still uses `GuardStatus.standalone_plugins`. |
| Packaging and tests | Removed package references to the deleted helpers and obsolete TLS mocks. Kept the existing checks for TDP download security and version metadata. Removed unused test scaffolding and imports. |

## Deliberately retained

- Public RPC methods in `main.py`, including those without a current UI caller;
  they remain externally reachable through Decky.
- Lifecycle hooks invoked through `getattr`, rollback CLI entry points, settings
  migrations, recovery journals, compatibility fields, and hardware fallbacks.
- `companion_updates.py` and `tdp_updater.py`: the former handles Companion ZIP
  downloads; the latter supports the pinned RyzenAdj download with TLS validation.
- Both gamescope scripts, which serve the supported display modes.
- The complete vendored pyudev library. Its optional GUI adapters are unused by
  Companion, but the project deliberately distributes an unmodified upstream copy.
- All npm dependencies, including `@types/react-dom`, which is consumed by
  `@decky/ui` declarations; the intentionally empty `requirements.txt`.
- Licenses, notices, frontend rebuild inputs, documentation, logo, and test suites.

## Verification

Local environment: Windows, Python 3.10.9, Node.js 24.18.0.

- Backend suite: 379 tests collected; 361 passed and 18 skipped, matching the
  original checkout's skip count.
- All nine frontend regression scripts passed.
- Type checking passed, including an additional run with `--noUnusedLocals`,
  `--noUnusedParameters`, and `--allowUnreachableCode false`.
- Frontend build, package payload check, and ZIP creation passed.
- The archive verification from `.github/workflows/checks.yml` passed, including
  CRC, required files, path checks, and byte-for-byte comparison with source files.
  The two deleted updater files are absent from the ZIP.
- `git diff --check` passed.

No further safe deletion was identified in the reviewed first-party code.
Hardware calls in tests are mocked. This cleanup has not been installed or tested
on the Legion Go 2, and the Linux CI matrix was not run locally.
