// SPDX-License-Identifier: BSD-3-Clause
// Types of firmware.mjs for the Decky panel (the Desktop app imports the module from QML).
type Component = { label: string; installed: string; available: string; newer: boolean };
export const PHASE_LABELS: Record<string, string>;
export const BUSY_LABELS: Record<string, string>;
export const RISK_NOTICE: { title: string; text: string };
export function installConfirm(status?: { components?: Component[] } | null): { title: string; ok: string; text: string };
