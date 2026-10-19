// SPDX-License-Identifier: BSD-3-Clause
// Types of fan.mjs for the Decky panel (the Desktop app imports the module from QML).
type Curve = [number, number][];
type Limits = { min_rpm: number; max_rpm: number; step: number };
export const STARTING_STUCK_MS: number;
export function curveName(name: string): string;
export function readingFresh(readings: { valid?: boolean; age_seconds?: number } | undefined, transportAge: number): boolean;
export function speedText(rpm: number | undefined, unit: string | undefined, maxRpm: number | undefined): string;
export function fanStatus(state: object | undefined, startingFor?: number): { headline: string; detail: string; starting?: boolean };
export function editPoint(curve: Curve, index: number, axis: 0 | 1, value: number, limits: Limits): Curve;
export function nudgeSpeed(rpm: number, delta: number, minRpm: number): number;
