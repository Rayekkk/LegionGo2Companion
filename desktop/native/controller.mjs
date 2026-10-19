// SPDX-License-Identifier: BSD-3-Clause
// Gyro & touchpad rules and texts, shared by the Desktop app and the Decky panel (src/controller.tsx imports this file).

export const SOURCE_LABELS = { system: "System default", combined: "Both controllers", left: "Left controller", right: "Right controller" };
export const sourceLabel = source => source ? SOURCE_LABELS[source] || "Unknown" : "Not confirmed";

/** "Not confirmed yet…" until the backend verifies Companion's own routing on this controller
 *  (status.controlled: same device, filters and gyro reporting, nothing pending), not only matching filters. */
export const sourceNote = status => `Which controller's motion Steam uses. System default hands control back to SteamOS.${
  status?.gyro_source && status.gyro_source !== "system" && !status.controlled ? " Not confirmed yet…" : ""}`;

/** The gyro source waits for InputPlumber, for another app to give the controller back, and for the controller test to end. */
export const sourceLocked = (status, testing) => !status?.available || !!status.conflict || !!testing;

const finite = value => typeof value === "number" && Number.isFinite(value);
/** "X 12 · Y -3 · Z 840" in raw sensor counts, or a dash when nothing arrived. */
export const vectorText = value => value
  ? ["x", "y", "z"].map(axis => `${axis.toUpperCase()} ${finite(value[axis]) ? value[axis] : "—"}`).join(" · ") : "—";
export const touchText = value => !value || typeof value.is_touching !== "boolean" ? "—"
  : !value.is_touching ? "no touch"
  : `touch at ${finite(value.x) ? value.x.toFixed(2) : "—"},${finite(value.y) ? value.y.toFixed(2) : "—"}`;
/** Report rate of a test stream; a silent stream is not evidence of a faulty sensor. */
export function streamText(stream) {
  const rate = !stream?.received ? "No reports yet."
    : `${stream.reports} reports · ${finite(stream.rate_hz) ? `${stream.rate_hz.toFixed(1)} Hz` : "rate unknown"}`;
  return stream?.error ? `${stream.error} ${rate}` : rate;
}
const reporting = value => value === true ? "On" : value === false ? "Off" : "Unknown";
export const reportingText = status => `left ${reporting(status?.imu?.actual?.left)} · right ${reporting(status?.imu?.actual?.right)}`;
