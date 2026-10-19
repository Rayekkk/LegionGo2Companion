// SPDX-License-Identifier: BSD-3-Clause
// Types of remap.mjs for the Decky panel (the Desktop app imports the module from QML).
type Status = { supported?: boolean; enabled?: boolean; active?: boolean };
export const BUTTONS: { id: "desktop" | "page"; label: string; note: string }[];
export function mappingEditable(status?: Status | null): boolean;
export function enableNote(status?: Status | null): string;
