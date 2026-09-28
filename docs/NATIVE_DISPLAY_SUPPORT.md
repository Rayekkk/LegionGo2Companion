# Native gamescope display support — 2026-09-28

## Assessment

Prefer native gamescope support once both content-driven HDR and the Legion Go 2
software-backlight path are installed. It implements the same broad policy as
Companion's Hybrid mode, but the compositor has direct access to frame colour
spaces and its output colour transforms. Companion should then retire its
display script and brightness/HDR writers while retaining the independent EDID
correction for games.

This is a source review, not a measurement of a console running these commits.
The user's report places them in SteamOS beta; channel membership and the date of
arrival in Stable are not inputs to the implementation.

## What the six commits do

| Commit | Practical effect |
| --- | --- |
| [6513879 — content-driven HDR](https://github.com/ValveSoftware/gamescope/commit/6513879ba33e5a8e4b89e173252f5604b0c2371d) | Separates HDR capability from active HDR output. With HDR enabled and content-driven behaviour selected, gamescope examines the last completed frame of each candidate application window. Any HDR frame keeps HDR output active, including while Steam UI has focus. HDR capability remains advertised while output is SDR, so a newly launched game can request HDR. Forced HDR output bypasses this policy. |
| [2dbc84d — luminance fallback](https://github.com/ValveSoftware/gamescope/commit/2dbc84d6650abb81d0914f0a772330e6598c23d1) | Missing Lua luminance fields fall back individually to the panel EDID; explicit script values still take precedence. This concerns gamescope's connector metadata, not compatibility of the EDID parser used by games. |
| [94667ca — Legion Go 2 profile](https://github.com/ValveSoftware/gamescope/commit/94667ca19cb3dc5ba4dbac752fbefdd7c2305504) | Adds a profile for SDC / 0x4301, with PQ capability and content-driven HDR. Colourimetry and luminance come from EDID. The 48–144 Hz refresh list and fixed-clock front-porch table match our profile; this is not a new refresh-rate range. |
| [00f8a85 — priority polling](https://github.com/ValveSoftware/gamescope/commit/00f8a859a622d774253a6d211317406c5fd79ebd) | Adds EPOLLPRI dispatch to gamescope's waitable objects, allowing the following change to react to sysfs brightness notifications. |
| [21c7e25 — software backlight](https://github.com/ValveSoftware/gamescope/commit/21c7e259c8203554311b846bbe5645b4467a4fe6) | Watches `actual_brightness`, divides it by `max_brightness`, and applies the resulting gain to the output colour LUTs when the connector requests software backlight, colour management is enabled, and output is PQ. This covers SDR and HDR input. Gain is clamped to at least 0.01. |
| [9eeb855 — enable it for Go 2](https://github.com/ValveSoftware/gamescope/commit/9eeb855516942a75bb0df8935773145d429a8095) | Sets `software_backlight = true` in the Legion Go 2 profile. A profile containing only content-driven HDR is therefore insufficient evidence of the complete replacement. |

## Comparison with our Hybrid mode

| Area | Companion Hybrid | Native implementation |
| --- | --- | --- |
| Switching decision | Reads X11 HDR-content feedback every 0.5 seconds and writes `GAMESCOPE_DISPLAY_HDR_ENABLED`. | Uses the compositor's completed application frames. No Companion polling or corrective writes are required. |
| Avoiding unnecessary transitions | A 3-second minimum PQ hold and 2-second continuous-SDR debounce, plus conflict backoff. | Keeps PQ while any candidate window still has an HDR frame. The supplied changes add no equivalent time hysteresis. |
| Advertising HDR to a game launched in SDR | Holds the debug force-support atom while Hybrid is active. | Advertises capability separately from actual output mode. The normal HDR-enabled setting still matters. |
| Dimming in PQ | Hybrid releases Companion's brightness forwarding and relies on Steam's HDR path. The separate PQ mode forwards the requested backlight value only for SDR content. | Uses the actual backlight ratio in colour transforms for both SDR and HDR input. |
| Display characteristics | Our Lua hardcodes one measured panel's primaries and luminance values. | Reads those values from the connected panel's EDID. |
| Control ownership | User Lua plus a Python process writing compositor atoms. | System profile and compositor implementation. |

The architectural advantage favours native support. It removes a competing
controller and integrates dimming where colour processing occurs. Hybrid's
timed debounce remains a real difference: source review alone cannot establish
which produces fewer visible transitions during loading screens or swapchain
recreation. Opening the Steam overlay is explicitly handled by the native
window-based policy. See the [HDR switching change](https://github.com/ValveSoftware/gamescope/commit/6513879ba33e5a8e4b89e173252f5604b0c2371d).

The native backlight path has limits. On hardware lacking suitable plane colour
management, dimming a single PQ layer can require composition instead of direct
scanout. Explicit replacement LUTs bypass the added gain. The gain floor is a
linear multiplier, not a claim about minimum measured nits or OLED black level.
Panel selection prefers the first `amdgpu_bl*` backlight; multi-device behaviour
and the difference between requested and actual brightness need console tests.
See the [software-backlight implementation](https://github.com/ValveSoftware/gamescope/commit/21c7e259c8203554311b846bbe5645b4467a4fe6).

## Why our Lua must be removed

Our profile assigns the same `known_displays.lenovo_legiongo2_oled` entry as the
new system profile. User scripts load after system scripts, so leaving ours in
place can replace the new profile and lose its new flags. Gamescope loads these
scripts at session startup; deleting the file does not unload the current
in-memory table. A Gaming Mode restart may therefore be required. See
[gamescope's script loader](https://github.com/ValveSoftware/gamescope/blob/9eeb855516942a75bb0df8935773145d429a8095/src/Script/Script.cpp).

Additionally, `GAMESCOPE_HDR_OUTPUT_FEEDBACK` now represents available HDR
capability rather than the current output transfer function. Continuing legacy
mode correction with the old interpretation would be unsafe. Our display
writers must be gated as a group, including direct RPCs and module re-enabling.

## Detection and retirement

Detection is local and read-only. The sole support criterion is the existence
of the system profile file:
`/usr/share/gamescope/scripts/00-gamescope/displays/lenovo.legiongo2.oled.lua`.
Its contents are not read or validated. Lua fields, hashes, gamescope version,
binary markers, and the SteamOS update channel are not detection requirements.
Upstream can change the profile or remove `content_driven` and
`software_backlight` without re-enabling Companion's legacy workaround.

A missing file means absent support. An inaccessible path or a non-file at the
profile path is inconclusive; an already retired fix remains blocked in that
case. After the profile disappears, legacy setup is exposed only after a Gaming
Mode session change, and the old fix is never silently reinstalled.

File presence establishes the installed support policy, not which profile the
running compositor loaded. The first handover therefore requests one Gaming
Mode restart, even without a legacy script. The request survives Decky reloads
and clears after a new gamescope session is observed while the system profile
exists. Completed retirement does not request another restart merely because
the detector implementation changes. No executable inspection is needed.

Retirement records recovery state before changing files or owned settings,
blocks legacy writes, removes recognised Companion Lua and its recognised
backup, and preserves foreign files. A foreign backup is not automatically
restored over the native configuration. Settings retain the user's legacy mode
preference and EDID choice. Cleanup failures remain visible and are retried;
session-bound ownership is never restored into a different gamescope process.
Restart information survives a Decky reload. Restarting Gaming Mode remains an
explicit user action because it closes games.

The Lua variants remain in the plugin package for older systems and ownership
recognition. No system-owned gamescope file is removed.

## EDID remains independent

None of these commits establishes that affected games can parse DisplayID 2.0
extensions correctly. Our separate workaround edits gamescope's published EDID
copy for those consumers. Gamescope reading panel luminance successfully does
not prove that a game's DXVK/libdisplay-info path also succeeds.

The EDID option therefore remains available and preserves its saved value with
native support, without a legacy mode or installed Lua, and while native cleanup
or a session restart is pending. Its existing path checks, backup and conditional
restoration remain in force. It never rewrites panel firmware.

## Validation scope

Automated tests cover profile presence regardless of contents, access errors,
migration recovery, session boundaries, preservation of foreign files, RPC
blocking, independent EDID operation, and frontend availability. Packaging must
include the detector and both legacy Lua variants.

After simplification, local validation passed: 426 backend tests (408 passed,
18 platform-dependent skips), all nine frontend suites, TypeScript checks,
frontend build, and package staging.

On 2026-09-28, SSH readback on the user's Legion Go 2 running SteamOS 3.9.2
confirmed retirement with the previous detector: owned Lua and backup removed,
no cleanup or restart pending, the native profile loaded by gamescope 3.16.30,
and the independent EDID copy reduced from 384 to 256 bytes with unchanged
base chromaticity and CTA metadata. This is not a device test of the simplified
detector or a visual acceptance test.

Physical validation remains outstanding: SDR/HDR transitions and overlays,
brightness at low and high levels, game loading screens, suspend/resume,
dock/undock, and EDID readback in an affected game. The implementation must not
report measured image-quality, power-use or latency improvements from mocked
tests.
