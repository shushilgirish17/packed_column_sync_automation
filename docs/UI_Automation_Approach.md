# Packed Column Sync — What "UI Automation" Means, and What We Have Proven

**Prepared for:** Hannah Pinkston, Lorraine Metzka, Keerthana Chandrasekaran
**Re:** comment on *Packed Column sync clarifications* — "What is the UI automation?
Is this just putting in the column details manually? If so then we should wait on
the feature request."
**Status:** technical findings from hands-on investigation on the UNICORN client.

---

## 1. Direct answer to the question

**No — it is not a person, or a robot, typing details into a form.**

"UI automation" here means a program driving the UNICORN desktop client through
**Microsoft UI Automation**, the accessibility framework built into Windows and
the same interface screen readers use. Controls are addressed by their stable
internal identifiers — for example the *New…* button on the Column Type tab is
`btnNewType` — not by screen position or pixel matching.

To be precise about what it is and is not:

| It is | It is not |
|---|---|
| A program addressing named controls via a Windows API | A macro replaying mouse coordinates |
| Data pulled automatically from Benchling | An operator re-keying values |
| Deterministic, logged, unit-testable | Screen scraping or OCR |
| Verifiable after every step | Fire-and-forget clicking |

No human types anything. The Benchling record is read, transformed by defined
mapping rules, and written into UNICORN through the same interface a user would
use — which is precisely why the application's own validation rules and audit
trail still apply.

**The honest caveat:** this *is* still the UI, so it is more fragile than a real
API and it needs qualification. The concern behind the question is a fair one.
Section 4 sets out what we found, and Section 3 explains why the choice may not
be as binary as "UI automation vs wait for Cytiva".

---

## 2. Why not the database directly

Ruled out, with vendor backing: the UNICORN technical documentation (p.143,
database overview) states **direct SQL access is not recommended**. The client
also connects to a **shared central production database** — Lorraine confirmed
"this is our active database".

Writing directly to it would bypass the application's validation and the audit
trail. Going through the application is the conservative option, not the risky
one.

---

## 3. Important: the Import route may not be closed

The clarifications document states:

> The only supported routes are importing the Cytiva-created column list through
> the Column Handling module, or creating a column manually in the same module.
> This means the Import option cannot be used for lab-packed columns.

**Our testing suggests this conclusion should be re-checked before it drives the
decision.** Evidence:

1. We exported a column type named **"0.46 x10.0cm Cytiva Source 15S"**. Despite
   the name, its XML shows it was **created by a Lilly user**
   (`CreatedBy: L006857_Medley`, 2026-02-10), with `IsPredefined: false` and
   `AccessScopeName: Global`. It is **not** a Cytiva-predefined entry — the
   "Predefined" scope is a separate category in UNICORN's own Access-label filter.
2. `Import…` sits directly beside `Export…` on the Column Type Parameters tab and
   is enabled.
3. We decoded the export format completely (Section 4.3), including both
   integrity checksums, and can generate byte-identical files.
4. The payload root element is `<ColumnTypes>` — **plural**. One file can carry
   many column types, which is exactly the shape a batch sync needs.

Cytiva's statement was that **no API** exists. That is true, and file-based import
is not an API — so the two statements are not in conflict. It is possible the
"cannot be used" inference is broader than what Cytiva actually meant.

**This is worth one test.** If Import accepts a generated file, the approach stops
being form-filling altogether and becomes *generate a file, import it* — far
simpler and far more robust than either alternative. If it is rejected, we have
lost an afternoon and fall back to Section 1.

**What we have not done:** we have not attempted an import. Producing a valid file
is not the same as UNICORN accepting one. That test needs sign-off (Section 6).

---

## 4. What has been accomplished

All findings below are measured on the live client, read-only unless stated.

### 4.1 The dialog is fully machine-readable

The Column Handling dialog exposes **690–719 elements**, read in **~0.3–1.4 s**.
Command buttons carry stable identifiers: `btnNewType`, `btnEditType`,
`btnImportType`, `btnExportType`, `btnNewColumn`, `btnImportColumn`.

Two locator rules were established the hard way:
- The dialog's own id and the combo/list/grid ids are **regenerated per run** —
  these must be addressed by name, not id.
- A tab's controls exist in the tree **only while that tab is selected**.

### 4.2 A safe interaction method was identified

This was the most important technical result. Three ways to click were tested:

| Method | Opens dialog | Effect on UNICORN |
|---|---|---|
| **WinAppDriver click** | yes, ~1.0 s | none — stays healthy |
| Synthetic mouse click | yes, ~1.0 s | none — stays healthy |
| Standard UIA `Invoke()` | yes | **blocks the whole accessibility layer** |

The third method looks like the obvious approach and is actively harmful: while it
runs, UNICORN's accessibility layer becomes unusable (measured: 127 ms → 65,276 ms
per read), and if the calling process is terminated mid-call it stays broken until
UNICORN is restarted. Throughout, the application looks completely normal to a
user — no error, no warning.

We hit this, diagnosed it, and built a **health check** (`canary.ps1`) that detects
it in ~0.4 s. A production runner would check this before every run and refuse to
start if degraded.

This matters for the decision: it is the kind of silent failure that would
otherwise be found in production at 2 a.m. It has been found, understood, and
made detectable **before** any build started.

### 4.3 The export/import file format is fully decoded

A column type export is a ZIP containing `ColumnType.xml` and `Manifest.xml`,
protected by two CRC-32 checksums — one over the payload, one over the manifest
with its own checksum attribute removed. Both were derived and verified against
two independent exports; our generator reproduces UNICORN's own manifests
**byte-for-byte**.

The XML contains **all nine fields the New Column Type window marks as required**,
plus `AccessScopeName`, which controls the Global/Personal visibility setting —
so that setting is handled declaratively rather than as a button to remember.

### 4.4 Duplicate prevention is solved

An open item from 28 July. The column type list (**555 entries**) is readable and
searchable, so checking before creating is a straightforward read. Whatever
definition of "duplicate" is agreed, the check is cheap.

### 4.5 A test sandbox exists

Column types have no per-user folder — a Global entry is visible to everyone. But
`AccessScopeName = Personal` is visible only to its creator and is filterable in
the dialog. Round-trip testing can therefore be done as **Personal**, then flipped
to Global for production, by changing one field.

---

## 5. Risks, stated plainly

| Risk | Control |
|---|---|
| Accessibility layer can be silently degraded | Detected in ~0.4 s by the canary; recovery is a client restart |
| UI changes on UNICORN upgrade could break locators | Locator rules documented; regression test needed per upgrade |
| Writes go to a shared production database | Test as Personal scope; a QA instance is still needed for validated work |
| Audit attribution | Entries are stamped with the logged-in account. Unattended runs need an agreed service identity — **open GxP question** |
| Cannot run as a Windows service | Requires a logged-in interactive session; needs a security decision on an unattended workstation |

---

## 6. What we are asking for

1. **A decision on the route** — proceed with UI automation this phase, or wait for
   the Cytiva feature request. Our recommendation is in Section 7.
2. **Permission for one controlled test**: create a **single Personal-scope column
   type via Import** on the current system. It is the smallest experiment that
   proves or disproves the entire approach, is invisible to other users, and is
   reversible.
3. **A test/QA UNICORN instance** for anything beyond that.
4. **The five value-source decisions** (Technique, pressures, flow rates). We agree
   with the assessment in the clarifications document that a maintained lookup per
   resin/hardware is the right model rather than operator entry — Hannah's
   suggestion of tying these to the resin library result table fits well.

---

## 7. Recommendation

**Run the Import test before deciding.** It is a few hours of work and it
determines which conversation we are having:

- **If Import works** — the approach is "generate a file, import it". Minimal UI
  interaction, batch-capable, and much of the fragility in Hannah's concern
  disappears. There would be little reason to wait for the feature request.
- **If Import is rejected** — then the choice is genuinely between form-filling
  automation and waiting. In that case waiting has real merit, and we would want to
  know Cytiva's timeline before committing.

Deciding now, without that test, means choosing between two options when there may
be a third that is better than both.

---

## Appendix — Open items not affected by this decision

Still required regardless of route: inactive-column handling (archive vs delete),
pre-packed vendor column scope, active↔inactive sync behaviour, naming convention
confirmation, and the approved route for UNICORN backend access (asked 26 Aug,
chased 31 Aug, still unanswered).
