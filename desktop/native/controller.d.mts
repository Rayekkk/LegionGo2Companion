// SPDX-License-Identifier: BSD-3-Clause
// Types of controller.mjs for the Decky panel (the Desktop app imports the module from QML).
type Source = "system" | "combined" | "left" | "right";
type Status = { available?: boolean; conflict?: boolean; gyro_source?: Source; controlled?: boolean;
  imu?: { actual?: { left?: boolean; right?: boolean } | null } };
type Vector = { x: number | null; y: number | null; z: number | null };
type Touchpad = { x: number | null; y: number | null; is_touching: boolean | null };
export const SOURCE_LABELS: Record<Source, string>;
export function sourceLabel(source: Source | null | undefined): string;
export function sourceNote(status?: Status | null): string;
export function sourceLocked(status: Status | null | undefined, testing: boolean): boolean;
export function vectorText(value: Vector | null | undefined): string;
export function touchText(value: Touchpad | null | undefined): string;
export function streamText(stream: { received: boolean; reports: number; rate_hz: number | null; error?: string } | null | undefined): string;
export function reportingText(status?: Status | null): string;
