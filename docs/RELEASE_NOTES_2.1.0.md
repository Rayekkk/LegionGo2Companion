# Legion Go 2 Companion 2.1.0

Released 2026-10-11.

### New: Fan Control

Build your own cooling curve directly in the Steam overlay, with direct control
through the console's embedded controller (EC). Live temperature and measured fan
speed sit alongside the editor.

- **An eight-point curve from 0 to 100°C.** Adjust the first seven points; the last stays fixed at 100°C and full speed. Points stay in temperature order, and the curve cannot decrease in speed as temperature rises.
- **Touch and controller editing.** Drag points with touch, or press **A** on the graph, select a point with **LB / RB**, and use the **D-pad** to change temperature and speed. **B** leaves point editing. Choose a speed edit step, or open the **full-screen curve editor** for more room.
- **RPM or percentage.** Choose the display unit and see actual measured fan speed separately from your requested curve. Live readings also work with firmware automatic control; 100% corresponds to the tested 4800 RPM ceiling.
- **Steadier cooling:** built-in hysteresis avoids chasing small temperature fluctuations. Speed rises quickly when needed, then falls more gradually as the device cools.
- **Three starting curves.** Silent, Balanced and Aggressive provide editable starting points. New settings use Balanced with one global curve; choosing a template changes the draft, and saving keeps it for later.
- **Optional power-preset assignments.** Keep the same global curve everywhere, or enable assignments to Companion's TDP presets, including game and battery/AC profile changes. Assignments start off and do not follow the separate SteamOS QAM power profiles.
- **You choose when custom cooling runs.** Saving a curve while control is off does not activate it. Enable Fan Control when ready; turning it off returns control to firmware automatic. After startup, plugin reload, sleep or a fault, custom control requires explicit re-enabling. Saved curves remain available, and the page reports blocked operation or incomplete recovery.
- **Independent recovery monitoring:** a separate watchdog helps release custom control back to firmware automatic if the controller stops responding. Stale or inconsistent readings and detected faults also trigger recovery; this depends on a working kernel and EC connection.

**Compatibility:** Fan Control is limited to the validated **Legion Go 2 8ASP2 /
83N0, BIOS RRCN16WW and EC 1.16**. Supported targets are **0 RPM (stop)** or
**1500–4800 RPM**; measured speed can vary from the target. Other hardware or
firmware variants are not enabled for this feature. Use only one fan controller at
a time, and return any other controller to firmware automatic before enabling it.

#### Fan Control in the Steam overlay

<a href="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/d81b0d741b4e64766f7d445cd13cff763e4865f2/docs/screenshots/fan-control.png"><img src="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/d81b0d741b4e64766f7d445cd13cff763e4865f2/docs/screenshots/fan-control.png" alt="Fan Control showing live temperature, measured fan speed and the saved curve" width="760"></a>

#### Full-screen curve editor

<a href="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/d81b0d741b4e64766f7d445cd13cff763e4865f2/docs/screenshots/fan-curve-editor.png"><img src="https://raw.githubusercontent.com/Rayekkk/LegionGo2Companion/d81b0d741b4e64766f7d445cd13cff763e4865f2/docs/screenshots/fan-curve-editor.png" alt="Full-screen fan curve editor with eight temperature and speed points" width="760"></a>

### Fixes and improvements

- **More responsive vibration edits:** rapid changes now keep the latest queued value for each control and profile, instead of replaying superseded values behind a slow hardware write. Closing the page still saves the final accepted change.
- **Wi-Fi recovery follows the connection:** power-save changes and recovery verify the active network profile, reducing the chance of restoring a previous connection's policy after switching networks. Unverified recovery remains pending.
- **Stricter OLED mode validation:** invalid mode requests are rejected before changing settings or installing the legacy display script, instead of silently selecting PQ.
- **A clearer user guide:** the README now explains controls, recovery and hardware limits, with screenshots of all nine modules, module management and the full-screen fan editor.
