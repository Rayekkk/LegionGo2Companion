// SPDX-License-Identifier: BSD-3-Clause
// Types of controller.mjs for the Decky panel (the Desktop app imports the module from QML).
type Source = "system" | "combined" | "left" | "right";
type Status = { available?: boolean; conflict?: boolean; gyro_source?: Source; controlled?: boolean;
  imu?: { actual?: { left?: boolean; right?: boolean } | null };
  sleep?: { available?: boolean; minutes?: number | null; error?: string } };
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
export const SLEEP_TITLE: string;
export const SLEEP_LABEL: string;
export const SLEEP_NOTE: string;
export const SLEEP_DEFAULT: string;
export function sleepText(minutes: number): string;
export function sleepShown(status?: Status | null): boolean;
export function sleepValue(status?: Status | null): number | "default";
export function sleepOptions(status?: Status | null): { id: number | "default"; label: string }[];
export function sleepChange(status: Status | null | undefined, value: unknown): value is number;
export function sleepNote(status?: Status | null): string;
