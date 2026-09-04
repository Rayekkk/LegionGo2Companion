<div align="center">

<h1>Legion Go 2 Companion</h1>

[![Version](https://img.shields.io/badge/version-0.4.5-C2410C?style=for-the-badge&labelColor=141417)](CHANGELOG.md)
[![Device](https://img.shields.io/badge/device-Legion_Go_2-6E40C9?style=for-the-badge&labelColor=141417)](#requirements)
[![Requires](https://img.shields.io/badge/requires-Decky_Loader-0969DA?style=for-the-badge&labelColor=141417)](https://decky.xyz)
[![License](https://img.shields.io/badge/license-BSD--3--Clause-424A53?style=for-the-badge&labelColor=141417)](LICENSE)

**Power, haptics, lighting, buttons, OLED and Wi-Fi in one Steam overlay.**
The Legion Go 2 controls you use every day, with saved profiles and automatic recovery after wake.

[Features](#features) · [Requirements](#requirements) · [Installation](#installation) · [Usage](#usage) · [How it works](#how-it-works) · [Troubleshooting](#troubleshooting)

</div>

---

## Features

| | |
|---|---|
| **TDP presets and custom limits** | Five presets, sustained power and burst headroom, with a live package-power reading |
| **Game and charging profiles** | Separate TDP settings for each game and its battery/AC states, applied without opening the menu |
| **CPU Boost and EPP** | Independent CPU controls, saved and checked against the kernel after each change |
| **Controller haptics** | Vibration intensity and modes, touchpad feedback, a test action and per-game vibration profiles |
| **Joystick lighting** | Solid, Breathing, Color cycle and Rainbow effects, with colour, brightness and supported speed controls |
| **Power-button light** | A separate saved toggle on the verified Legion Go 2 firmware |
| **Extra-button remapping** | Independent actions for the left controller's Desktop and Page buttons, including F1-F12 and Disabled |
| **OLED display modes** | Hybrid, PQ and Gamma 2.2, with brightness-slider forwarding and corrected HDR metadata for games |
| **Wi-Fi band preference** | Prefer 5/6 GHz while keeping 2.4 GHz available, plus a manual rescan/reconnect action |
| **Settings recovery** | Imports standalone settings, restores saved controls after startup/wake and checks for later drift |

---

## Requirements

| Requirement | Details |
|---|---|
| Device | Lenovo Legion Go 2; the combined plugin is validated on the Ryzen Z2 Extreme model |
| OS | SteamOS in Gaming Mode, with gamescope |
| Plugin loader | [Decky Loader](https://decky.xyz) |
| TDP | Lenovo's `lenovo-wmi-other` firmware interface; Extras also needs the downloaded `ryzenadj` helper |
| Haptics and RGB | The `hid-lenovo-go` controller driver and its supported sysfs controls |
| OLED brightness | Samsung `AMS881KB01-0`, identified by EDID manufacturer `SDC` and product `0x4301` |
| Button remapping | SteamOS' existing InputPlumber service; validated with version 0.78.0 |
| Wi-Fi preference | The supported MediaTek MT7922 / `mt7921e` configuration with NetworkManager and iwd |

Each page checks the interface it needs and reports when a control is unavailable. Support
in an individual standalone plugin does not make another handheld a supported Companion
device.

> [!NOTE]
> The power-button light is limited to the verified **83N0 / RRCN16WW** BIOS and ACPI
> layout. If the firmware does not match, that control stays unavailable until the new
> layout has been checked.

---

## Installation

**1.** Install [Decky Loader](https://decky.xyz) if it is not already installed.
**2.** Build `LegionGo2Companion-<version>.zip` using the instructions below. No GitHub release has been published yet.
**3.** Remove the overlapping standalone plugins listed below, retaining their settings.
**4.** In Gaming Mode, open the **Quick Access Menu → Decky → Settings → Developer**.
**5.** Choose **Install Plugin from ZIP** and select the archive.

The archive contains one `LegionGo2Companion` folder. Decky installs it with the privileges
needed by the hardware controls; normal use requires no terminal commands.

> [!IMPORTANT]
> Companion pauses **all modules** if **LeGoTDP**, **LeGo Vibe Control** or
> **LeGo2 Brightness Fix** is installed. Uninstall the listed plugins in Decky Settings,
> keep their saved settings, then restart Decky or the console. Disabling them is not
> enough. WiFi Optimizer and HueSync are not included in this installation check.

### Conflict protection

The backend checks before startup and every component request, and scans installed
plugins every five seconds while running. A newly detected conflict blocks further
requests and stops all module workers after an in-flight operation finishes. The frontend
replaces hardware controls with a paused screen and the names of the detected plugins.

Detection uses both installation-folder names and plugin manifests, so renamed copies
also count. Removing a conflict does not silently restart partially stopped modules:
**Check again** confirms the current state, then a Decky or console restart starts a fresh
instance. User profiles are kept. A plugin whose identity cannot be read safely also
pauses Companion until the installation can be verified.

### Moving from standalone plugins

Companion imports TDP, vibration, display and Wi-Fi settings from their existing Decky
settings folders. Existing Companion values take precedence; missing values can be filled
from the old files. Importing does not modify or delete those source files.

Keep the standalone settings folders when removing their plugins. If those files have
already been deleted, there is nothing to import. Companion keeps its own settings outside
the installed plugin folder, so replacing the plugin files preserves them.

Open **OLED Display** after installation. It may ask for a display mode and offer
**Restart Game Mode** if the installed script differs from the one gamescope has loaded.

<details>
<summary><b>Building from source</b></summary>

<br>

Requires Node.js 18+ and a ZIP tool: `zip` on Linux, or 7-Zip on Windows. From the source
directory:

```bash
npm ci
npm run typecheck
npm run build      # compile the frontend into dist/
npm run package    # create LegionGo2Companion-<version>.zip
```

Packaging uses the existing frontend build, so run **build before package**. Install the
result through Decky rather than copying a checkout containing `node_modules` into its
plugins directory.

</details>

---

## Usage

Open **Legion Go 2 Companion** from the Decky menu. The main page shows a short status for
each hardware area; selecting one opens its controls. **All Controls** returns to that
overview. Choosing an item from a dropdown keeps the current page open.

### TDP

**Enable** gives Companion control of the TDP limits. Turning it off releases that control;
CPU Boost and EPP are independent controls.

**Presets** apply immediately. The Legion Go 2 ladder is:

| Preset | SPL | SPPT | FPPT |
|---|---|---|---|
| Minimum | 5 W | 5 W | 10 W |
| Silent | 8 W | 10 W | 15 W |
| Balanced | 15 W | 18 W | 25 W |
| Performance | 25 W | 28 W | 35 W |
| **Max** | **35 W** | **37 W** | **45 W** |

The available range is checked against the firmware. In **Custom**, SPL sets sustained
power; SPPT and FPPT add longer and shorter burst headroom above it. **Apply TDP** commits
the slider values.

**Per Game Profile** stores the limits for the running game. **Separate AC Profile** gives
that game independent battery and charging settings. The editing buttons select which
profile you are changing; connecting the charger selects which one the backend applies.

**CPU Boost** controls the kernel's boost setting. **EPP** runs from 0% towards performance
to 100% towards power saving, in 10% steps. Both choices are saved separately from TDP and
reapplied after startup or resume.

**Current TDP** shows the limits alongside live package power. Package power is the APU
reading, not the entire console's battery drain.

> [!WARNING]
> **Extras** extends the Custom range up to 50 W through `ryzenadj`, beyond the normal
> firmware range. Higher requested limits can increase heat and power use and may still
> be constrained by the hardware. Enable this only if you intend to use that range.

### Vibration

Choose the handle **Intensity** and **Mode**, then configure touchpad vibration separately.
**Test Vibration (0.5s)** gives a short check of the selected handle settings.

With a game running, enable **Per Game Profile** to save its haptic settings. Returning to
the global profile uses the global choices again. Controller reconnection and wake trigger
reapplication when the driver becomes available.

### RGB Lighting

**Enable RGB control** lets Companion manage the rings. **Joystick ring lights** switches
their output on or off. Select an effect, then adjust the controls relevant to it:

| Effect | Controls |
|---|---|
| **Solid** | Colour and brightness |
| **Breathing** | Colour, brightness and speed |
| **Color cycle** | Brightness and speed |
| **Rainbow** | Brightness and speed |

Colour uses hue and saturation; the preview shows the current choice. Changes are saved as
you make them, with slider writes grouped while you move a control. Animations run in the
controller firmware.

**Power button light** is a separate setting. **Restore lighting state from before
Companion** returns the captured lighting state and releases Companion's lighting control.

### Button Remapper

Enable remapping, then select an action independently for **Desktop button** and **Page
button**. Choices include Steam menus, the on-screen keyboard, screenshots, window
switching, navigation keys and F1-F12. **Disabled** makes that button produce no action.

**Default** restores the captured system behaviour for that button. Disabling remapping
restores the mappings Companion still owns, while preserving unrelated profile entries
and changes made elsewhere.

### OLED Display

Choose a mode on first run, or use **Switch Display Mode** later:

| Mode | Behaviour | Trade-off |
|---|---|---|
| **Hybrid** *(recommended)* | Uses Gamma 2.2 for SDR and switches to PQ for HDR content | The panel briefly blanks during a mode transition |
| **PQ** | Keeps the panel in its HDR transfer function | SDR dimming is handled through gamescope and can lift the apparent black floor |
| **Gamma 2.2** | Keeps direct panel brightness control | HDR highlights are tone mapped into the lower luminance range |

**Brightness slider** forwards Steam's slider only when the panel is in PQ without HDR
content. When Steam is already controlling HDR brightness, this part stands aside.

**EDID for games** corrects gamescope's published display metadata so affected games can
read the panel's luminance values. Restart an already running game to let it read the
updated metadata.

> [!NOTE]
> Hybrid and PQ share a display script and can switch immediately. Moving to or from
> Gamma 2.2 can require **Restart Game Mode**, because gamescope loads that script when
> the session starts. The page reports when a restart is needed.

### WiFi

**Prefer 5/6 GHz** changes iwd's band preference system-wide. It keeps 2.4 GHz available
when that is the connection the device can use; it does not pin the adapter to one band
or access point.

**Rescan and reconnect to 5/6 GHz** makes a bounded attempt to move the current connection
to a suitable higher-band access point. It briefly interrupts the connection. The page
shows the actual connected band and the result, so enabling the preference is not mistaken
for a completed switch.

---

## How it works

### One interface, separate hardware controls

Companion brings together the TDP, vibration, display and Wi-Fi implementations with RGB
and button-remapping pages. Each backend owns its hardware interface and settings; the
Decky entry point coordinates startup, shutdown and calls from the frontend.

| Area | System interface |
|---|---|
| TDP | Lenovo WMI within the firmware range; the verified `ryzenadj` helper for Extras |
| CPU Boost / EPP | Kernel CPU policy controls under sysfs |
| Live package power | RAPL energy counters |
| Haptics / joystick RGB | `hid-lenovo-go` sysfs controls |
| Button mappings | InputPlumber's D-Bus profile API |
| OLED | A gamescope display script, X properties and gamescope's EDID copy |
| Wi-Fi | NetworkManager and iwd configuration |

Button remapping reuses InputPlumber rather than installing another input service. The
display fix changes the EDID copy published by gamescope, not the panel's firmware.

### Saving, wake and drift

Settings use atomic replacement with backup/recovery handling. A failed TDP settings save
attempts to restore the previous effective target, and a delayed vibration edit is rejected
if its game/profile context has changed.

The backend continues managing saved profiles while Quick Access is closed. TDP checks
for changed limits and retries failed profile transitions with backoff. RGB and remapping
normally verify state once per minute; for the first 30 seconds after startup or wake they
check every five seconds to catch a controller or service that appears late. Matching
lighting and mappings do not need another hardware write or profile reload.

Wi-Fi recovery journals are stored persistently so a reboot cannot erase an unfinished
transaction. Recovery stops on a conflicting external change instead of overwriting it.

### Background work

Page-specific periodic status refreshes pause while Quick Access is hidden. Decky's
`alwaysRender` keeps the selected page mounted during a native dropdown, preserving its
navigation state without keeping those refresh timers running.

RGB effects run on the controller. Backlight notifications are event-driven where the
kernel supports them; gamescope state still needs periodic checks, with the shorter
interval used by Hybrid. Background profile and recovery checks remain active because
they are needed outside the menu. There is no fixed CPU percentage or battery-life claim:
the cost depends on the enabled controls, mode and system build.

---

## Troubleshooting

<details>
<summary><b>Companion says all modules are paused</b></summary>

<br>

LeGoTDP, LeGo Vibe Control or LeGo2 Brightness Fix is still installed. Uninstall the named
plugins from Decky Settings, retain their settings, and restart Decky or the console.
**Check again** can confirm that the conflict is gone, but restarting is still required.
Keep backup copies outside Decky's plugin directory: renaming a folder does not hide its
manifest from the guard. If the message says verification failed, repair the unreadable or
incomplete plugin installation before restarting.

</details>

<details>
<summary><b>A hardware control is unavailable</b></summary>

<br>

Read the reason on that page. Haptics and RGB need the controller driver's matching
controls; remapping needs InputPlumber; Wi-Fi needs the supported network configuration.
The power-button light also checks the exact BIOS and ACPI layout. Installing Companion
does not install a missing kernel driver or make another firmware layout compatible.

</details>

<details>
<summary><b>Extras is enabled, but only the normal TDP range is available</b></summary>

<br>

The `ryzenadj` helper may be unavailable. The standard WMI range continues to work, and
the saved Extras choice and profiles are retained. Connect to the internet and use the
download-retry control on the TDP page.

In the extended range, the SMU can constrain the request. The displayed SPL is the applied
target; the Strix Point STAPM reading is not a reliable direct read-back of that target.

</details>

<details>
<summary><b>The OLED page says standby, or a game still has the old HDR values</b></summary>

<br>

Brightness forwarding is supposed to stand aside when the panel is outside PQ or an HDR
game is using Steam's own brightness path. Check the separate EDID status for metadata
correction. A game launched before the correction may keep its old values until restarted.
If the page requests a Game Mode restart, the display script has not taken effect yet.

</details>

<details>
<summary><b>Wi-Fi stays on 2.4 GHz, or reports recovery required</b></summary>

<br>

A preference cannot create an available 5/6 GHz network. Check the live band and the result
of the rescan action. A recovery warning instead means an interrupted operation could not
be restored safely, usually because the network configuration changed elsewhere. Keep the
recovery journal and include the reported reason when diagnosing it.

</details>

---

## Development

```bash
npm ci
npm run typecheck      # check frontend types
npm run test:frontend  # navigation and pending-setting lifecycle checks
npm test              # Python backend tests
npm run build         # compile the frontend
npm run package       # package the current build
```

Backend tests require Python. Linux-specific permission and symlink checks skip on
Windows. Hardware calls are mocked in the unit suite; console testing is still needed
for startup, resume, game transitions and native Steam UI behaviour.

The release archive includes frontend source/build inputs, license notices and the bundled
pyudev source. The optional RyzenAdj executable is downloaded separately and checked
against its pinned hash. See [SOURCE.md](SOURCE.md) for frontend rebuilding and
[CHANGELOG.md](CHANGELOG.md) for version history.

---

## Credits

- [LeGoTDP](https://github.com/Rayekkk/LeGoTDP), [LeGo Vibe Control](https://github.com/Rayekkk/LeGo-Vibe-Control), [LeGo2 Brightness Fix](https://github.com/Rayekkk/LeGo2BrightnessFix) and the unpublished WiFi Optimizer Go 2 - the standalone foundations
- Ally Vibe Control by [piyush-tyagi-13](https://github.com/piyush-tyagi-13/ally-vibe-control), with original per-game profile work by M4ttiA - inherited MIT-licensed vibration code
- [HueSync](https://github.com/honjow/HueSync) by honjow and Steam Deck Homebrew - reference for the lighting interface and Go 2 sysfs controls
- [RyzenAdj](https://github.com/FlyGoat/RyzenAdj), [pyudev](https://github.com/pyudev/pyudev), [InputPlumber](https://github.com/ShadowBlip/InputPlumber) and [Decky Loader](https://github.com/SteamDeckHomebrew/decky-loader) - the supporting tools and interfaces

Full attribution and component licenses are recorded in [NOTICE](NOTICE).

---

## License

BSD 3-Clause - see [LICENSE](LICENSE). Inherited vibration code retains its
[MIT notice](LICENSE.MIT), and HueSync attribution is preserved in
[LICENSE.HUESYNC](LICENSE.HUESYNC). Third-party terms and source information are listed
in [NOTICE](NOTICE) and [SOURCE.md](SOURCE.md).

---

<div align="left">

*Vibe coded with AI assistance 🤖*

</div>
