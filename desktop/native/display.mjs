// SPDX-License-Identifier: BSD-3-Clause
// Display texts, shared by the Desktop app and the Decky panel (src/display.tsx imports this file).

export const EDID_FIX_LABEL = "Fix HDR brightness in games";
export const EDID_FIX_NOTE = "Games see the screen's real HDR brightness. Restart the game after changing.";

/** SteamOS older than 3.9 has no native profile for this screen; the HDR fix works either way. */
export const steamosNotice = state => state?.steamos_update_needed
  ? "Update SteamOS to 3.9 or newer, which supports this screen natively. The HDR brightness fix below still works." : "";

/** What the HDR brightness fix is doing now. */
export function edidStatus(state) {
  if (!state?.edid_fix) return "Off";
  if (!state.edid_patched) return "Not active yet";
  return Number(state.edid_game_nits) > 0 ? `On · games see ${Number(state.edid_game_nits).toFixed(0)} nits` : "On";
}
