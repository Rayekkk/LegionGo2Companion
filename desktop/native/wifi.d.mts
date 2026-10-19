// SPDX-License-Identifier: BSD-3-Clause
// Types of wifi.mjs for the Decky panel (the Desktop app imports the module from QML).
export type Policy = "off" | "five_six_no_24" | "five_six_only" | "six_ghz_only";
export type WifiAction = "off" | "prefer" | "strict" | "high" | "six" | "sixOnly";
export interface WifiStatus {
  success: boolean;
  connected?: boolean;
  settings?: { device_family?: string; device_label?: string; driver?: string; band_policy?: Policy };
  live?: {
    ssid?: string;
    connection_state?: "connected" | "connecting" | "disconnected" | "unknown";
    connection_uuid?: string;
    band_policy?: Policy;
    band_policy_restore_available?: boolean;
    six_ghz_only_supported?: boolean;
    frequency?: string;
    channel?: string;
    wifi_backend?: string;
    high_band_discovery_pending?: boolean;
    band_policy_error?: string;
    recovery_required?: boolean;
    recovery_errors?: string[];
  };
  drift?: Record<string, boolean>;
  error?: string;
  message?: string;
}
export const POLICY_LABELS: Record<Policy, string>;
export const POLICY_ACTIONS: Record<Policy, WifiAction>;
export const CONFIRMATIONS: Record<"five_six_only" | "six_ghz_only", { title: string; ok: string; text: string }>;
export const CHANGED: string;
export const UNVERIFIED: string;
export function bandName(frequency?: string | number | null): string;
export function savedPolicy(status?: WifiStatus | null): Policy;
export function inactivePolicy(status?: WifiStatus | null): boolean;
export function canRun(status: WifiStatus | null | undefined, action: WifiAction): boolean;
export function actionShown(status: WifiStatus | null | undefined, action: "band" | "high" | "six"): boolean;
export function bandOptions(status?: WifiStatus | null): { id: Policy; label: string }[];
export function bandNote(status?: WifiStatus | null): string;
export function wifiNotice(status?: WifiStatus | null): [string, string] | null;
export function networkName(status?: WifiStatus | null): string;
export function linkText(status?: WifiStatus | null): string;
export function backendName(status?: WifiStatus | null): string;
export function driverText(status?: WifiStatus | null): string;
export function frequencyText(status?: WifiStatus | null): string;
export function wifiSummary(status?: WifiStatus | null): string;
export function operationVerified(action: WifiAction, before: WifiStatus | null | undefined, after: WifiStatus | null | undefined): boolean;
