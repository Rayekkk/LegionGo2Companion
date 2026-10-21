// SPDX-License-Identifier: BSD-3-Clause
// Update check and device report texts, shared by the Desktop app and the Decky panel (src/updates.tsx imports this file).

export const DEVICE_REPORT_TITLE = "Save device report";
export const DEVICE_REPORT_DESCRIPTION = "Saves a text file to your Desktop with this console's hardware and firmware details for a bug report. No serial numbers, network names or user names.";
/** The outcome of save_device_report: where the file is, or why it is not. */
export const deviceReportResult = result => result?.success === true && result.path ? `Saved to ${result.path}`
  : result?.error || "The device report could not be saved. Try again.";

/** [headline, detail] for the last successful updates_check; before one, how to check. */
export function updateStatus(check, currentVersion) {
  if (!check?.success) return ["GitHub releases", `Installed version: ${currentVersion}. Check for updates when you need them.`];
  if (check.no_release) return ["No public release yet", `Installed version: ${currentVersion}. A download will appear after a release is published on GitHub.`];
  if (check.update_available) {
    if (!check.download_available) return ["New release found", `Version ${check.latest_version} has no downloadable plugin ZIP yet. Check again later.`];
    const size = check.size && check.size > 0 ? ` (${(check.size / 1048576).toFixed(1)} MB)` : "";
    return ["Update available", `Version ${check.latest_version}${size}. Download the release ZIP from GitHub.`];
  }
  if (check.latest_version && check.latest_version !== currentVersion) {
    return ["No newer release", `Installed: ${currentVersion}. Latest public release: ${check.latest_version}.`];
  }
  return ["Up to date", `Version ${currentVersion} is the latest public release.`];
}
