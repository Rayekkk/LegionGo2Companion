// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/frontend/about.tsx): change it there, then `kit sync`.
//
// Sources: DeckyEQ src/updates.tsx (UpdateController per plugin instance with dispose, the
// reason a newer release has no ZIP, ~/ paths, a failed re-check drops the old offer), LegionGo2Companion
// src/updates.tsx and desktop/native/updates.mjs (D2 answers wrapped as state, the download checks the version the
// button showed, failures in the console, the About page order and texts), docs/UI.md §3.
// Guarantees: one request at a time, and none after dispose(); a check that fails (refused, rejected with an Error or
// a string, no answer) shows "Could not complete" with its error and offers no download from an earlier check; a download runs only for
// the version the last check offered (and the button showed), and is confirmed only when the backend returns a path
// for exactly that version; a re-check keeps a download of the same version, a failed one keeps it on screen; an answer
// without update_available is a failure, never "Up to date"; answers are read as dk_update's own
// ({ok, ...fields}) or wrapped as D2 state ({ok, error, state}); the states and texts are UI.md §3's table; a saved
// path inside `home` shows as ~/…; AboutPage is always info first, then the plugin's own sections, Rayek's store,
// Updates last. StoreSection switches Decky's store channel to Rayek's store only after the user confirms a dialog
// that says what changes and how to go back; it writes the address before the channel (a failure in between leaves
// the channel as it was) and reports success only when both read back as set; when Decky's settings cannot be read
// or written it shows the manual steps instead; once selected it offers no button.
// Deliberately not here: automatic checks, a beta channel, installing (Decky installs the ZIP), RPC names, a way
// back to the official store (Decky's own Store channel setting does that).
import { PanelSection, PanelSectionRow } from "@decky/ui";
import { ReactNode, useEffect, useState } from "react";
import { ActionButton, confirmAction, infoRow, notify } from "./ui";

/** dk_update's check answer (DeckyKit D2). */
export interface UpdateCheck {
  ok: boolean;
  current_version: string;
  latest_version?: string;
  update_available: boolean;
  download_available: boolean;
  asset_name?: string;
  size?: number;
  checked_at?: number;
  no_release?: boolean;
  /** Why a newer release cannot be downloaded yet (e.g. no verified ZIP). */
  message?: string;
  error?: string;
}

export interface UpdateDownload { ok: boolean; path?: string; version?: string; sha256?: string; error?: string }

/** dk_update's answer as it is, or wrapped as D2 state ({ok, error, state}). */
type Answer<T> = T | { ok: boolean; error?: string; state?: Omit<T, "ok" | "error"> };
export interface UpdateClient {
  check(): Promise<Answer<UpdateCheck>>;
  download(expectedVersion: string): Promise<Answer<UpdateDownload>>;
}

interface UpdateState { busy: "check" | "download" | null; check?: UpdateCheck; download?: UpdateDownload; error?: string }

const unwrap = <T extends { ok: boolean; error?: string }>(answer: Answer<T> | null | undefined): T | undefined =>
  !answer || typeof answer !== "object" ? undefined
    : "state" in answer && answer.state && typeof answer.state === "object"
      ? { ...answer.state, ok: answer.ok, error: answer.error } as T : answer as T;

/** What a failed request says: Decky rejects with an Error or a plain string. */
const reason = (error: unknown, fallback = "The update request failed. Try again.") => (typeof error === "string" ? error
  : error && typeof error === "object" && "message" in error ? String(error.message) : "").trim() || fallback;

/** A request belongs to the plugin instance: switching pages or closing the QAM keeps its progress, and dispose()
 *  keeps old requests away from a reloaded plugin. `log` names the plugin in console warnings. */
export class UpdateController {
  private state: UpdateState = { busy: null };
  private listeners = new Set<() => void>();
  private closed = false;
  constructor(private client: UpdateClient, private log = "") {}
  getSnapshot = () => this.state;
  subscribe = (listener: () => void) => {
    this.listeners.add(listener);
    return () => { this.listeners.delete(listener); };
  };
  dispose() { this.closed = true; this.listeners.clear(); }
  private publish(state: UpdateState) {
    if (this.closed) return;
    this.state = state;
    this.listeners.forEach(listener => listener());
  }
  /** `expected`: the version the Download button showed; another one means the check changed meanwhile. */
  async run(kind: "check" | "download", expected = this.state.check?.latest_version) {
    if (this.closed || this.state.busy) return;
    const offer = this.state.check;
    if (kind === "download" && (!offer?.update_available || !offer.download_available || !expected
      || offer.latest_version !== expected)) return;
    this.publish({ ...this.state, busy: kind, error: undefined });
    try {
      if (kind === "check") {
        const result = unwrap(await this.client.check());
        if (!result?.ok) throw new Error(result?.error || "Could not check GitHub releases. Try again.");
        // An answer without the release facts must not read as "Up to date".
        if (typeof result.update_available !== "boolean") throw new Error("GitHub sent no release details. Try again.");
        this.publish({ ...this.state, check: result, error: undefined,
          download: this.state.download?.version === result.latest_version ? this.state.download : undefined });
      } else {
        const result = unwrap(await this.client.download(expected!));
        if (!result?.ok || !result.path || result.version !== expected) {
          throw new Error(result?.error || "Could not confirm the downloaded ZIP. Try again.");
        }
        this.publish({ ...this.state, download: result });
      }
    } catch (error) {
      if (this.log && !this.closed) console.warn(`[${this.log}] updates.${kind}_failed`);
      // A failed re-check must not keep offering the previous result's download.
      this.publish({ ...this.state, check: kind === "check" ? undefined : this.state.check,
        error: reason(error) });
    } finally {
      this.publish({ ...this.state, busy: null });
    }
  }
}

/** A path inside the home folder as ~/…: the full path names the user. */
export const homePath = (path: string, home = "") => {
  const base = home.replace(/\/+$/, "");
  return base && path.startsWith(`${base}/`) ? `~/${path.slice(base.length + 1)}` : path;
};

export const INSTALL_HINT = "To install it, turn on Developer mode in Decky settings → General, then use Developer → Install plugin from ZIP.";

/** [label, detail] of UI.md §3's table for the last successful check; before one, the installed version. */
export function updateStatus(check: UpdateCheck | undefined, installed: string): [string, string?] {
  if (!check) return [`Installed version ${installed}`, "Check for updates when you need them."];
  if (check.no_release) return ["No public release yet"];
  if (check.update_available) {
    if (!check.download_available) return ["New release found", check.message
      ? `Version ${check.latest_version}: ${check.message.replace(/\.$/, "")}. Check again later.`
      : `Version ${check.latest_version} has no plugin ZIP yet. Check again later.`];
    const size = check.size && check.size > 0 ? ` (${(check.size / 1048576).toFixed(1)} MB)` : "";
    return ["Update available", `Version ${check.latest_version}${size}.`];
  }
  return check.latest_version && check.latest_version !== installed ? ["No newer release"] : ["Up to date"];
}

/** The updater: status, Check for updates, Download X, the error and the saved ZIP. `install` replaces the hint. */
export function UpdateSection({ controller, version, home = "", install = INSTALL_HINT }:
  { controller: UpdateController; version: string; home?: string; install?: string }) {
  // A subscription, not useSyncExternalStore: the plugins' vm test harness has only the classic hooks.
  const [view, setView] = useState(controller.getSnapshot);
  useEffect(() => {
    setView(controller.getSnapshot());
    return controller.subscribe(() => setView(controller.getSnapshot()));
  }, [controller]);
  const offer = view.check, latest = offer?.latest_version;
  // A check of another version drops the download (run()); a failed check keeps it on screen.
  const saved = view.download?.path ? view.download : undefined;
  const [label, detail]: [string, ReactNode?] = saved?.path
    ? [`${saved.version} downloaded`, <><div style={{ overflowWrap: "anywhere" }}>Saved to {homePath(saved.path, home)}.</div>
      <div style={{ marginTop: 6 }}>{install}</div></>]
    : updateStatus(offer, offer?.current_version || version);
  return <PanelSection title="Updates">
    {infoRow(label, detail)}
    <PanelSectionRow><ActionButton disabled={view.busy !== null} onClick={() => void controller.run("check")}>
      {view.busy === "check" ? "Checking GitHub…" : "Check for updates"}
    </ActionButton></PanelSectionRow>
    {offer?.update_available && offer.download_available && latest && !saved && <PanelSectionRow>
      <ActionButton disabled={view.busy !== null} onClick={() => void controller.run("download", latest)}>
        {view.busy === "download" ? "Downloading ZIP…" : `Download ${latest}`}
      </ActionButton>
    </PanelSectionRow>}
    {view.error && infoRow("Could not complete", <div role="alert">{view.error}</div>)}
  </PanelSection>;
}

/** Rayek's plugin store (RayekDeckyStore): Decky's Custom store channel at this address. */
export const STORE_URL = "https://decky.rayek.workers.dev";
const CUSTOM = 2;  // Decky's Store enum: Default 0, Testing 1, Custom 2
const STORE = "Rayek's store";
const MANUAL = `Set it by hand: Decky settings → General → Store channel → Custom, then enter ${STORE_URL}.`;

// Decky's own settings routes (what its Store channel setting uses): not plugin API, so a failure shows MANUAL.
type Loader = { call(route: string, ...args: unknown[]): Promise<unknown> };
const loader = () => {
  const backend = (window as unknown as { DeckyBackend?: Loader }).DeckyBackend;
  if (!backend?.call) throw new Error("Decky's settings are not available.");
  return backend;
};
const storeSelected = async () => {
  const backend = loader();
  const [store, url] = await Promise.all([backend.call("utilities/settings/get", "store", null),
    backend.call("utilities/settings/get", "store-url", null)]);
  return store === CUSTOM && typeof url === "string" && url.trim().replace(/\/+$/, "") === STORE_URL;
};

/** Rayek's store: a button that makes it Decky's store channel, after the user confirms. */
export function StoreSection() {
  const [selected, setSelected] = useState<boolean | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { storeSelected().then(setSelected, () => setSelected(false)); }, []);
  const use = async () => {
    setBusy(true);
    setError("");
    try {
      const backend = loader();
      // The address first: should the channel write fail, Decky keeps the store it had.
      await backend.call("utilities/settings/set", "store-url", STORE_URL);
      await backend.call("utilities/settings/set", "store", CUSTOM);
      if (!await storeSelected()) throw new Error("Decky did not keep the new store.");
      setSelected(true);
      notify(STORE, "Selected. Decky's Store tab now lists Rayek's plugins.");
    } catch (e) {
      console.warn(`[${STORE}] store_switch_failed`);
      setError(reason(e, "Could not change Decky's store."));
    } finally {
      setBusy(false);
    }
  };
  // "OK" instead of a verb (UI.md §6): the owner's choice for this dialog (2026-10-30).
  const ask = () => confirmAction("Change Decky's store channel?", <div style={{ whiteSpace: "pre-line" }}>{
    `You are changing Decky's default store channel to ${STORE} (${STORE_URL}).\n` +
    "While it is selected, plugins from the official store get no update offers.\n" +
    "This can be undone: set Store channel to Default in Decky settings → General."}</div>, "OK", () => void use());
  return <PanelSection title={STORE}>
    {selected
      ? infoRow("Selected", "Decky's Store tab lists Rayek's plugins and offers their updates.")
      : <>
        {infoRow("Install and update from Decky", "Make Rayek's store Decky's store channel, so the Store tab lists these plugins.")}
        <PanelSectionRow><ActionButton disabled={busy || selected === undefined} onClick={ask}>
          {busy ? "Switching store…" : `Use ${STORE}`}
        </ActionButton></PanelSectionRow>
      </>}
    {error && infoRow("Could not complete", <div role="alert">{error}<div style={{ marginTop: 6 }}>{MANUAL}</div></div>)}
  </PanelSection>;
}

/**
 * The About page (UI.md §3), the same everywhere: the plugin's name, `Version X` and one sentence on what it does;
 * Author (Rayek · license · source, then license notes); credits; the plugin's own sections (`children`, e.g. a
 * device report); Rayek's store; Updates at the very bottom.
 */
export function AboutPage({ name, version, description, license, source, notes, credits = [], children, updates, home, install }: {
  name: string; version: string; description: string; license: string; source?: string; notes?: string;
  credits?: Array<[string, string]>; children?: ReactNode; updates?: UpdateController; home?: string; install?: string;
}) {
  return <>
    <PanelSection title={name}>
      {infoRow(`Version ${version}`, description)}
      {infoRow("Author", <>{["Rayek", license, source].filter(Boolean).join(" · ")}{notes && <div>{notes}</div>}</>)}
      {credits.map(([label, text]) => <div key={label}>{infoRow(label, text)}</div>)}
    </PanelSection>
    {children}
    <StoreSection />
    {updates &&<UpdateSection controller={updates} version={version} home={home} install={install} />}
  </>;
}
