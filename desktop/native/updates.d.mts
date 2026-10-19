// SPDX-License-Identifier: BSD-3-Clause
// Types of updates.mjs for the Decky panel (the Desktop app imports the module from QML).
export interface UpdateCheck {
  success: boolean;
  current_version: string;
  latest_version?: string;
  update_available: boolean;
  download_available: boolean;
  size?: number;
  checked_at?: number;
  no_release?: boolean;
  error?: string;
}

export function updateStatus(check: UpdateCheck | null | undefined, currentVersion: string): [string, string];
