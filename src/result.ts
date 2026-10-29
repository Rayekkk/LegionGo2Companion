// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/frontend/result.ts): change it there, then `kit sync`.
//
// Sources: DeckyLS (Result<T>, settle), LegionGo2Companion/Ayaneo3Companion src/shared.tsx (errorText with a
// fallback for anything without a message, toasts that can never throw), DeckyEQ src/types.ts (Result shape with a
// success message).
// Guarantees: a call wrapped in settle() never rejects and keeps the call's own answer type (extra fields such as
// DeckyReshade's launch_options stay typed); errorText() always returns a non-empty string; toastError() never
// throws, even when Steam's toaster is not ready (it falls back to console.error).
// Deliberately not here: retries, i18n, a write queue (writeQueue.ts).
import { toaster } from "@decky/api";

/** What every backend method returns (DeckyKit D2): ok, a message for the user when not, the new state. */
export interface Result<T> { ok: boolean; error?: string; state?: T; message?: string }

/** The message of anything a call rejected with (a dead backend, a Decky error); fallback when there is none. */
export const errorText = (e: unknown, fallback = "Unknown error"): string => {
  const text = e === undefined || e === null ? "" : typeof e !== "object" ? String(e)
    : "message" in e ? String((e as { message: unknown }).message) : "";
  return text.trim() || fallback;
};

/** A call that never rejects: a failed call becomes { ok: false, error }, like a refusal from the backend. The answer
 * keeps its type R, whose fields beyond ok/error must be optional (a failure has none of them). */
export const settle = <R extends Result<unknown>>(call: Promise<R>): Promise<R> =>
  call.then(r => (r && typeof r === "object" ? r : { ok: false, error: "The backend sent no answer." } as R),
    (e: unknown) => ({ ok: false, error: errorText(e) }) as R);

/** Show a refused or failed result as a toast; returns the result's state. Never throws. */
export function toastError<T>(r: Result<T>, title: string): T | undefined {
  if (!r.ok) {
    try {
      toaster.toast({ title, body: r.error || "Unknown error" });
    } catch (e) {
      console.error(title, r.error, e);
    }
  }
  return r.state;
}
