// SPDX-License-Identifier: BSD-3-Clause
// Types of updates.mjs for the Decky panel (the Desktop app imports the module from QML).
/** The state of a successful updates_check (dk_update's fields). */
export interface UpdateCheck {
  current_version: string;
  latest_version?: string;
  update_available: boolean;
  download_available: boolean;
  size?: number;
  checked_at?: number;
  no_release?: boolean;
  message?: string;
}

/** The saved report's state, or the error of a refusal. */
export interface DeviceReport {
  path?: string;
  error?: string;
}

export const DEVICE_REPORT_TITLE: string;
export const DEVICE_REPORT_DESCRIPTION: string;
export function deviceReportResult(result: DeviceReport | null | undefined): string;
export function updateStatus(check: UpdateCheck | null | undefined, currentVersion: string): [string, string];
