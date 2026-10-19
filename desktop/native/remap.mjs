// SPDX-License-Identifier: BSD-3-Clause
// Button mapping rules and texts, shared by the Desktop app and the Decky panel (src/remap.tsx imports this file).

export const BUTTONS = [
  { id: "desktop", label: "Desktop button", note: "Default: SteamOS behaviour." },
  { id: "page", label: "Page button", note: "Default: takes a screenshot." },
];

/** The button choices apply only while mapping is supported and on; Restore defaults needs support only. */
export const mappingEditable = status => status?.supported === true && status.enabled === true;

export const enableNote = status => `Change what the two extra buttons on the left controller do.${
  status?.enabled && !status.active ? " Waiting to reapply…" : ""}`;
