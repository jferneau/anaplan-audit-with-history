# [Part 1] Enhanced Reporting of the Anaplan Audit Log Summary

*AnaplanOEG · Operational Excellence Group*

## Intended Audience

**Level of Difficulty:** Beginner-to-Intermediate
A non-technical reader can follow the narrative. The companion
[Part 2](community/part-2-enhancing-anaplan-audit-log-data-extraction-with-streamlined-python-solutions.md)
is the implementation guide for engineers.

**Resources Required:**

- **Internal Expertise:** Tenant Administrator, security/compliance
  lead, and an Anaplan model builder for the reporting model.
- **Tools Needed:** the open-source [Anaplan Audit History](https://github.com/qkeddy/anaplan-audit-history)
  Python solution (v3), Python 3.13, a host with network access to
  `auth.anaplan.com` and `api.anaplan.com`.
- **Access Requirements:** Tenant Auditor role for reading audit
  events, Workspace Administrator on the target reporting workspace,
  and either Basic, Certificate, or OAuth credentials.

**Estimated Level of Effort (LoE):** Reading time ~10 minutes; rolling
the solution out to production: 2-4 hours.

---

## Introduction

As organizations continue to scale on Anaplan, maintaining defensible
security and compliance posture is no longer optional. The Anaplan
Audit Log is the system of record for **who did what, when, and from
where** across the tenant — login activity, role changes, model access,
imports and exports, integrations, workflows, comments, and BYOK key
operations. Used well, it answers compliance reviewers' questions in
minutes and gives security teams the signal they need for SIEM
ingestion. Used poorly — or not at all — it is the single thing every
auditor asks for and no one has on hand.

The challenge is that Anaplan's audit log is exposed via a paginated
REST API, retained for a limited window (~30 days), and contains
event-type codes that don't carry their own descriptions. To turn
that into a report a business audience can read, you need to:

1. Extract events on a schedule before they roll off the 30-day window.
2. Blend events with the metadata they reference (users, workspaces,
   models, actions, processes, CloudWorks integrations).
3. Map event-type codes to human-readable messages.
4. Persist a long-term store.
5. Load the result into a model your auditors can actually open.

That is the problem the **Anaplan Audit History** open-source solution
exists to solve. This article (Part 1) covers the **why** and the
high-level outcome. [Part 2](community/part-2-enhancing-anaplan-audit-log-data-extraction-with-streamlined-python-solutions.md)
covers the **how** — the Python solution, the architecture, the APIs
used, and the deployment steps.

---

## Understanding the Audit Log

The Anaplan audit log exposes eight categories of events, each shipped
under its own filter in the Audit UI:

| Category | What it tracks |
|---|---|
| User activity (`USR-*`) | Logins, model access, dashboard views, exports, role changes, password changes, UX page activity, IP-list imports |
| Access control (`AUTHZ-*`) | Role assigned/unassigned, access granted/denied |
| Connection management (`CONN-*`) | SAML/SSO connection lifecycle |
| Encryption / BYOK (`DSM-*`) | Key pair, symmetric key, guardpoint events |
| CloudWorks integrations (`INT-01..07`) | CloudWorks connection and run events |
| Anaplan Data Orchestrator (`INT-50..66`) | ADO pipelines, dataspaces, schedules, connections |
| Workflow (`WF-*`) | Workflow task lifecycle + template lifecycle |
| Comments (`COMMENT-*`) | Comment added, deleted, exported |
| Forecaster (`FRCST-*`) | Data Collection, Forecast Model, and Forecast Action operations |

Each event includes a stable identifier, a timestamp, the user who
took the action, the IP address and user-agent of the device, the
affected object, success/failure, and a category-specific
`additionalAttributes` payload (workspace ID, model ID, app ID, page
ID, role IDs, and so on).

This is rich data — but it is rich data that lives 30 days, in a
paginated API, with no built-in reporting layer.

---

## Solution Overview

The Anaplan Audit History solution is a Python application that runs
on any Linux/macOS host (a small VM, a container, an OEG appliance, or
an analyst's workstation). It runs on whatever schedule you choose —
typically every 1-4 hours for the audit pipeline and nightly for the
optional Model History pipeline — and performs the following work
end-to-end under a process-level lock:

```
1. Authenticate to Anaplan (Basic, Certificate, or OAuth)
2. Fetch metadata: users · workspaces · models · actions · processes ·
   CloudWorks integrations · the activity-code catalog
3. Fetch audit events since the last successful run
4. Persist everything in a local SQLite database (deduplicated by event ID)
5. Run a multi-join SQL query that maps every event row to its
   workspace, model, user, and human-readable message
6. Upload the result to a dedicated Anaplan Audit Reporting Model
```

When the optional Model History feature is enabled, the orchestrator
also triggers the per-model change-history export in every in-scope
model, normalizes the dynamic CSV into a fixed flat schema, retains it
for a configurable window (default 2 years), and loads it into a
separate Model History Reporting Model.

The output is a report-ready set of Anaplan modules that an analyst
can build dashboards against, and a SQLite database an engineering
team can query directly for ad-hoc forensics.

---

## What's New in v3

This article was originally published in 2023 alongside v1 of the
solution. v3 — released in 2026 — is a substantial rewrite that
addresses production realities our customer base has surfaced over
three years of deployment:

- **Forward-compatible audit catalog.** Anaplan continues to add
  event categories — Anaplan Data Orchestrator (`INT-50..66`),
  Workflow templates (`WF-1000..1006`), Comments (`COMMENT-*`),
  the Forecaster `FRCST-*` codes that replaced legacy `PIQ-*`,
  UX board / worksheet / report tracking, IP-list import/export.
  v3 ships the full ~220-code catalog and the underlying schema grows
  itself when Anaplan adds new `additionalAttributes` keys.
- **Production reliability.** Transient API failures are retried with
  exponential backoff. Auth tokens refresh proactively. An OS-level
  process lock prevents two scheduler invocations from clobbering
  each other.
- **Optional Model History pipeline.** Per-model change history,
  normalized and retention-purged, loaded into a dedicated reporting
  model. Independent toggle, parallel export, idempotent re-runs.
- **Operator-friendly.** Distinct exit codes per failure category,
  structured JSON logs ready for SIEM ingestion, `--dry-run` mode,
  automatic backups before any purge.

A complete change summary is available in the v3 repository under
[`docs/whats-new-in-v3.md`](https://github.com/qkeddy/anaplan-audit-history/blob/v3/docs/whats-new-in-v3.md).

---

## Why this matters

The audit log is the single most-asked-for evidence artifact in
post-incident reviews, compliance audits, and access-recertification
campaigns. Customers that have automated audit-log reporting:

- Answer "who exported X" in under a minute, not under an hour.
- Catch privileged-role drift on a routine cadence instead of when a
  compliance reviewer asks for it.
- Feed Anaplan events directly into existing SIEM and identity-
  governance pipelines.
- Carry historical data well past Anaplan's 30-day retention window,
  with no operational overhead.

In short: the audit log already exists. v3 makes it useful.

---

## Conclusion and Next Steps

The Anaplan Audit Log carries the data security and compliance teams
need — but it isn't useful by itself. The Anaplan Audit History v3
solution turns it into a report-ready dataset on a schedule, with
production reliability, full coverage of Anaplan's current event
catalog, and an optional per-model change-history pipeline.

To deploy the solution, continue to [Part 2](community/part-2-enhancing-anaplan-audit-log-data-extraction-with-streamlined-python-solutions.md),
which walks through the architecture, the Anaplan REST APIs used, and
the step-by-step deployment process.

Got feedback on this content? Let us know in the comments below.

---

**Author:** Jon Ferneau, Data Integration Principal, Operational
Excellence Group (OEG)

**Original v1 credit:** Quin Eddy (@QuinE) and Chris Stauffer,
Anaplan OEG — for the 2023 v1 release on which v3 is based.
