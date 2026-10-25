// SPDX-License-Identifier: BSD-3-Clause
// Types of display.mjs for the Decky panel (the Desktop app imports the module from QML).
type State = { steamos_update_needed?: boolean; edid_fix?: boolean; edid_patched?: boolean; edid_game_nits?: number };
export const EDID_FIX_LABEL: string;
export const EDID_FIX_NOTE: string;
export function steamosNotice(state?: State | null): string;
export function edidStatus(state?: State | null): string;
