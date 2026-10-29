// SPDX-License-Identifier: BSD-3-Clause
// Shared from DeckyKit (shared/frontend/ui.tsx): change it there, then `kit sync`.
//
// Sources: LegionGo2Companion src/shared.tsx (infoRow, detailsRows with the repair actions above the facts,
// statusPlaceholder with a spinner, confirmAction), LegionGo2Companion src/index.tsx and Ayaneo3Companion src/ui.tsx
// (SectionLink with a status note, the page header's one focus handoff, icons that inherit Steam's focus color),
// LegionGo2Companion src/tdp_api.tsx (toasts that never throw), DeckyEQ src/shared.tsx (ActionButton that fills the
// row, bDestructiveWarning, a dismissed dialog that puts its control back, dialogs shown through the plugin's own
// tracker), LegionGo2Companion src/index.tsx and DeckyReshade src/shared.tsx (PageShell: B goes back, names stay
// inside the QAM's width), DeckyLS src/index.tsx (the faint warning surface), docs/UI.md §2–§6, §9–§10.
// Guarantees: infoRow is focusable, so the D-pad reaches read-only rows; ActionButton fills its row; SectionLink shows
// icon, title, a one-line summary (and a note under it) and a chevron, and takes the focus only when it mounts with
// `focus`, never on a later render (a poll, the QAM opening again); PageHeader is the page's first row, `‹ <back>` in a
// section titled with the page, and focuses its button only on entry with `focus`; PageShell keeps a page inside
// the QAM's width and, with `onBack`, makes B go back one page (UI.md §2) instead of closing the menu; statusPlaceholder is never empty: a spinner while loading,
// then the label, the error and "Try again"; detailsRows puts the actions above the facts, so the last row is an
// always-enabled focusable field; confirmAction always has "Cancel" as its second button, a destructive action gets
// Steam's warning, and Cancel or B runs onCancel; notify never throws (Steam's toaster may not be ready) and logs only
// its title, never the text (it may hold a path), titles the toast with the module and keeps it 5 s when the text is longer than one sentence; staleNote says how old the shown
// values are; colors are the one palette of every panel (UI.md §10), except the accent a plugin may keep as its own.
// Deliberately not here: page state, polling and RPC (result.ts), i18n, plugin icons, Steam focus outside the panel.
import { toaster } from "@decky/api";
import { ButtonItem, ConfirmModal, Field, Focusable, PanelSection, PanelSectionRow, showModal, Spinner } from "@decky/ui";
import { ComponentProps, Fragment, ReactElement, ReactNode, useEffect, useRef } from "react";

/** The one palette of every panel and graph: no plugin writes its own hex colors. */
export const colors = {
  // The default accent (Steam's blue): the curve, a live state, the frame of a graph being edited. A plugin may keep its
  // own accent (owner's decision 2026-10-29, e.g. DeckyEQ's green) and passes it where an accent is drawn (CurveGraph).
  accent: "#66c0f4",
  ok: "#73e3c0", warning: "#f5c96b", error: "#ff9ca6",
  text: "#dcdedf", muted: "#afb7c0",
  surface: "#111923", okSurface: "#26382f", errorSurface: "#472f2e", warningSurface: "#f5c96b1f",
  curve: { grid: "#ffffff14", zero: "#ffffff50", border: "#ffffff24", label: "#91a1b2", reading: "#eef2f7",
    selected: "#ffffff", live: "#f5c96b" },
} as const;

/** A read-only row; `focusable` keeps it reachable with the D-pad. */
export const infoRow = (label: ReactNode, description?: ReactNode) =>
  <PanelSectionRow><Field focusable label={label} description={description} /></PanelSectionRow>;

/** A standalone action: Steam's default ButtonItem is an inline, fixed-width field, this one fills the row. */
export const ActionButton = (props: ComponentProps<typeof ButtonItem>) =>
  <ButtonItem {...props} layout="below" childrenContainerWidth="max" />;

/** A 24×24 stroke icon (paths only) that takes Steam's text and focus color. */
export const Icon = ({ children }: { children: ReactNode }) =>
  <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
    strokeLinecap="round" strokeLinejoin="round" style={{ display: "block", width: "1.2em", height: "1.2em", flexShrink: 0 }}>
    {children}
  </svg>;
export const Chevron = () =>
  <svg aria-hidden="true" focusable="false" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"
    style={{ display: "block", width: "1em", height: "1em", flexShrink: 0 }}><path d="m9 18 6-6-6-6" /></svg>;

/** A row that opens a page: icon, title, a one-line summary of its state (an error replaces it), chevron.
 *  `focus`: this row was just left and the panel is visible (useQuickAccessVisible); read only when the row mounts. */
export function SectionLink({ title, description, note, icon, focus = false, onOpen }:
  { title: string; description: ReactNode; note?: ReactNode; icon?: ReactNode; focus?: boolean; onOpen(): void }) {
  const ref = useRef<HTMLDivElement>(null);
  // Returning from a page puts the D-pad back on its row once; polls, dropdowns and reopening the QAM never move it.
  useEffect(() => { if (focus) ref.current?.focus(); }, []);
  return <PanelSectionRow>
    <Field ref={ref} label={title} icon={icon && <Icon>{icon}</Icon>}
      description={note ? <>{description}<div style={{ fontSize: "11px", opacity: 0.7, marginTop: "2px" }}>{note}</div></> : description}
      bottomSeparator="standard" verticalAlignment="center" childrenLayout="inline" childrenContainerWidth="min"
      focusable highlightOnFocus onActivate={onOpen}><Chevron /></Field>
  </PanelSectionRow>;
}

/** A page's first rows: its title and `‹ <back>`, which takes the focus on entry when `focus` (the panel is visible). */
export function PageHeader({ title, back, onBack, focus = false }: { title: string; back: string; onBack(): void; focus?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  // One focus handoff on page entry, never on a poll or a dropdown dismissal.
  useEffect(() => { if (focus) ref.current?.querySelector<HTMLButtonElement>("button")?.focus(); }, []);
  return <PanelSection title={title}>
    <PanelSectionRow><div ref={ref}><ActionButton onClick={onBack}>‹ {back}</ActionButton></div></PanelSectionRow>
  </PanelSection>;
}

const SHELL = { width: "100%", maxWidth: "100%", minWidth: 0, overflowX: "hidden", boxSizing: "border-box" } as const;

/** A page: long names stay inside the QAM's width; with `onBack` (a subpage), B goes back one page instead of closing
 *  the menu (UI.md §2). The main page passes no `onBack`, so B closes the QAM as in Steam. */
export const PageShell = ({ children, onBack, rootRef, className }:
  { children: ReactNode; onBack?(): void; rootRef?: { current: HTMLDivElement | null }; className?: string }) =>
  <div ref={rootRef} className={className} style={SHELL}>
    {onBack ? <Focusable onCancelActionDescription="Back" onCancelButton={event => {
      event.preventDefault();
      event.stopPropagation();
      onBack();
    }}>{children}</Focusable> : children}
  </div>;

/** A page's first state: a spinner while loading; once it failed, `label` (e.g. "Fan Control unavailable"), the error
 *  and "Try again". Never an empty or endlessly waiting page (UI.md §4). */
export const statusPlaceholder = ({ title, label, error, onRetry, retryLabel = "Try again" }:
  { title?: string; label: string; error?: string | null; onRetry(): void; retryLabel?: string }) =>
  <PanelSection title={title}>
    <PanelSectionRow>{error ? <Field focusable label={label} description={<div role="alert">{error}</div>} /> : <Spinner />}</PanelSectionRow>
    {error && <PanelSectionRow><ActionButton onClick={onRetry}>{retryLabel}</ActionButton></PanelSectionRow>}
  </PanelSection>;

/** "Last read 12s ago · Retrying…": under values kept after a failed refresh (UI.md §4). */
export const staleNote = (ageMs: number) => {
  const seconds = Math.max(0, Math.round(ageMs / 1000));
  return `Last read ${seconds < 60 ? `${seconds}s` : `${Math.floor(seconds / 60)} min`} ago · Retrying…`;
};

/** A page's last rows: "Show <what>" and, once shown, the rare repair actions and then the technical facts,
 *  so the last row stays a focusable, always-enabled field. */
export const detailsRows = (shown: boolean, toggle: () => void, rows: Array<[ReactNode, ReactNode]>, actions?: ReactNode,
  what = "details") => <>
  <PanelSectionRow><ActionButton onClick={toggle}>{shown ? `Hide ${what}` : `Show ${what}`}</ActionButton></PanelSectionRow>
  {shown && actions}
  {shown && rows.map(([label, description], index) => <Fragment key={index}>{infoRow(label, description)}</Fragment>)}
</>;

/**
 * Asks first: only for actions that delete data, close apps or the game, can drop a connection, raise hardware limits
 * or flash firmware (UI.md §6). `title` is a question, `ok` repeats its verb ("Delete", "Allow 30 W"), never "OK".
 * The dialog hides the Quick Access menu, so `onOK` usually runs unseen: report its outcome with notify().
 * `description` may be a node (e.g. lines kept with white-space: pre-line); it reaches the dialog as it is.
 * `onCancel` also runs when the dialog is closed with B (e.g. to put a toggle back); `show` is the plugin's own
 * showModal when it tracks its dialogs.
 */
export const confirmAction = (title: string, description: ReactNode, ok: string, onOK: () => void,
  { destructive = false, onCancel, show = showModal }: { destructive?: boolean; onCancel?(): void; show?(modal: ReactElement): unknown } = {}) =>
  show(<ConfirmModal strTitle={title} strDescription={description} strOKButtonText={ok} strCancelButtonText="Cancel"
    bDestructiveWarning={destructive} onOK={onOK} onCancel={onCancel} />);

/** A toast titled with the module ("Fan Control", "DeckyEQ"); longer than one sentence stays 5 s. Never throws. */
export const notify = (title: string, body: string) => {
  try {
    toaster.toast({ title, body, ...(/[.!?…]\s+\S/.test(body.trim()) ? { duration: 5000 } : {}) });
  } catch {
    console.error(`[${title}] toast failed`);  // Not the text: it may hold a path.
  }
};
