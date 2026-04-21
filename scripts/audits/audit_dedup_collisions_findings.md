# Dedup-collision audit — findings (task #10)

Companion to `scripts/audits/audit_dedup_collisions.sql` and migration
`server/migrations/012_idempotency_keys.sql`.

## Where the audit was run

- **Production**: migration 012 has not been deployed (verified:
  `patient_conditions.natural_key` does not exist in production, and no
  `_pre_012_*_backup` tables are present). The audit script therefore has
  nothing to scan there yet.
- **Development**: migration 012 has been applied, and the four backup
  tables created by it (`_pre_012_conditions_backup`,
  `_pre_012_medications_backup`, `_pre_012_encounters_backup`,
  `_pre_012_screenings_backup`) are populated. Dev was used as the best
  available proxy for what the dedup will collapse on production once 012
  ships.

> Side note: the original audit script referenced
> `_pre_012_clinical_events_backup` and
> `_pre_012_behavioral_screenings_backup`, which are not the names
> migration 012 actually creates. Those references were corrected to
> `_pre_012_encounters_backup` / `_pre_012_screenings_backup` so the
> script runs end-to-end.

## Headline numbers (dev)

| Table                    | Collision groups | …with diverging fields |
|--------------------------|-----------------:|-----------------------:|
| patient_conditions       |               33 |                      5 |
| patient_medications      |               15 |                      0 |
| clinical_events          |               22 |                      0 |
| behavioral_screenings    |                0 |                      0 |

Only `patient_conditions` produced flagged groups (5). All five belong
to a single patient (`2cfaa9f2-3f47-44be-84e2-16f3a5dc0bbb`).

## Per-group review

| # | Code / display fingerprint | Date       | Rows | What differed                                       | Distinct event? |
|---|---------------------------|------------|-----:|-----------------------------------------------------|-----------------|
| 1 | "Cholelithiasis" (no code) | 2017-09-19 |   18 | clinical_status: `inactive` vs `resolved`           | No — status drift across re-ingestions |
| 2 | "Elevated glucose" (no code) | 2015-02-26 | 12 | clinical_status: `inactive` vs `resolved`           | No — status drift |
| 3 | "GERD" (no code)          | 2017-04-25 |   10 | clinical_status: `inactive` vs `resolved`           | No — status drift |
| 4 | E66.01 (Morbid obesity)   | 2015-02-26 |    3 | three free-text displays for the same ICD-10 code; one row missing the `system` URI | No — same diagnosis under three label variants |
| 5 | K80.20 (Cholelithiasis)   | 2017-09-19 |    2 | ICD-10 canonical display vs colloquial "Cholelithiasis" | No — same code, different label |

In every flagged group the underlying clinical fact is the same; the
divergence is in `clinical_status` (which churns as the same condition
is re-ingested over time) or in the free-text `display` (different
sources spelling the same diagnosis differently). These are exactly
the situations the natural key in migration 012 was designed to
collapse — they are not distinct same-day clinical events.

## Decision

**No widening migration is required at this time.**

- The natural-key formula in `012_idempotency_keys.sql` is behaving as
  intended. None of the dev collisions represent genuinely distinct
  same-day events; collapsing them is correct behaviour.
- Production has not yet received migration 012, so there is no
  production data at risk and no rows to restore from a backup.
- If, after 012 is deployed to production, the audit script later
  surfaces flagged groups that *do* look like distinct events, the
  follow-up migration would be `015_widen_natural_key.sql` (note: the
  task description suggested `013_…`, but `013_staleness_band_config.sql`
  and `014_atom_pressure_refresh_schedule.sql` are already taken).

## How to re-run

```bash
psql "$DATABASE_URL" -f scripts/audits/audit_dedup_collisions.sql
```

The script is read-only and safe to run in any environment that has the
`_pre_012_*_backup` tables.

## Re-check log

### 2026-04-21 — production still pre-migration

Re-ran the existence checks against the production read-replica (task #12):

| Check                                                            | Result |
|------------------------------------------------------------------|-------:|
| `patient_conditions.natural_key` column exists in production     |      0 |
| `patient_medications.natural_key` column exists in production    |      0 |
| `_pre_012_*_backup` tables present in production                 |      0 |

Migration 012 has therefore still not shipped to production. There is no
production dedup to audit and no backup snapshots to compare against, so
the audit script was not run and no clinician review was triggered.
**Status unchanged from the original task #10 review.** This task should
be re-opened the next time migration 012 is deployed; at that point run
`audit_dedup_collisions.sql` against `$DATABASE_URL` (production) and
have a clinician review any flagged groups before deciding whether
`015_widen_natural_key.sql` is needed.
