// SPDX-License-Identifier: BSD-3-Clause
// Battery rules and texts, shared by the Desktop app and the Decky panel (src/battery.tsx imports this file).

/** A reported percentage, or a dash for a missing or impossible value. */
export const levelText = value => typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100
  ? `${Math.round(value)}%` : "—";

/** One controller: "left 80%", "left 80% (detached)" or "left —". */
const controllerShort = (side, value, connection) => {
  const level = levelText(value);
  return `${side} ${level}${connection === "detached" && level !== "—" ? " (detached)" : ""}`;
};
export const controllersText = levels => `${controllerShort("left", levels?.left, levels?.connection_left)} · ${
  controllerShort("right", levels?.right, levels?.connection_right)}`;

/** The saved choice while Companion manages charging, otherwise what the battery reports. */
export const protectionChecked = status => status?.managed && typeof status.requested_enabled === "boolean"
  ? status.requested_enabled === true : status?.enabled === true;

/** The switch waits for a known actual state and for a pending recovery (Restore previous charging mode). */
export const protectionLocked = status => !status?.supported || typeof status.enabled !== "boolean" || status.recovery_pending === true;

export function protectionNote(status) {
  const waiting = status?.managed && typeof status.requested_enabled === "boolean" && status.requested_enabled !== status.enabled;
  return `Stops charging at about 80% to protect the battery.${typeof status?.enabled !== "boolean" ? " Current state unknown."
    : waiting ? " Waiting for the battery to confirm…" : ""}`;
}
