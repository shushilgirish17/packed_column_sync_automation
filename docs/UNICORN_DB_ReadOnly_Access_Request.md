# Request: read-only access to the UNICORN database (reconciliation only)

**To:** Hannah Pinkston; Lorraine Metzka; Jonathan Gutierrez
**From:** [Zifo team]
**Re:** Packed Column sync — read-only DB access for duplicate detection

## What we're asking for

Read-only (SELECT) access to the UNICORN production database, for the automation
service account only, scoped to duplicate detection and reconciliation:

| | |
|---|---|
| Server | `AZBRDLABAPPS.AZR.LILLY.COM\UNICORN` |
| Database | `UNICORN_PD` |
| Access level | **read-only** — `db_datareader` (or a SELECT-only role on the column-type tables) |
| Encryption | Encrypt connection = on, Trust server certificate = on (matches the client config) |
| Used by | the sync service account, not an individual |

(Connection details taken from the UNICORN Service Tool → Database Server
Connection tab on the lab workstation.)

## Why read-only, and why only for this

To be explicit about scope: **we are not asking for write access, and we are not
proposing to write to the database.** All column-type creation and updates go
through the UNICORN application (Import / Edit in Column Handling), which we have
already validated end to end. That keeps UNICORN's own validation and audit
trail intact — consistent with the Technical Manual (p.143), which advises
against direct SQL access.

The one place a database read genuinely helps is **duplicate prevention** — an
open item from the 28 July requirements discussion. Checking whether a column
type already exists currently means driving the UI to search 555+ entries.
A single read-only query does the same instantly, server-side, without consuming
a UNICORN licence seat or an interactive session.

## What we would do with it

- Query existing column types (name, resin, dimensions) to decide, before each
  sync, whether to **create** (Import) or **update** (Edit) — preventing the
  duplicate entries raised as a concern in the requirements discussion.
- Reconcile Benchling records against what is already in UNICORN.

Nothing is written over this connection.

## There is a documented, vendor-sanctioned way to grant this

Cytiva's *UNICORN Database in network — Configuration guide* (29723939 AE),
**Appendix E**, documents adding a **Windows user or group** to the UNICORN
database via SSMS:

> Security → Logins → New Login → (Windows user/group); then in the database,
> Security → Users → Properties → Membership, assign the role.

The guide assigns `db_datareader` **and** `db_datawriter` there for UNICORN's
own account. For us, the same procedure with **`db_datareader` only** gives
exactly the read-only reconciliation access requested — no write role, and via
a Windows/AD login, so there is no shared SQL password to manage.

(Note: Appendix E frames Windows-only auth as a UNICORN 7.11+ feature for how
*UNICORN itself* connects; this system is 7.10. That version note does not
affect adding a separate read-only Windows login for our querying — that is a
standard SQL Server admin action, independent of UNICORN's own connection mode.)

## What we need from you

1. Confirmation that read-only access is acceptable for this purpose.
2. The route and contact to provision it — likely a DBA/sysadmin performing the
   Appendix E steps with `db_datareader` only. (This was left open in the 26 Aug
   thread — "how can the Zifo team obtain UNICORN backend access".)
3. Whether access should be granted to a dedicated AD group or to the service
   account directly.

Happy to walk through the exact queries beforehand if that helps the review.
