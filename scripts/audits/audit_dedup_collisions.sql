-- audit_dedup_collisions.sql
--
-- Surfaces "suspicious" merges performed by migration 012
-- (server/migrations/012_idempotency_keys.sql).
--
-- The new natural_key collapses rows that share
--   (patient_id, code, date)
-- to a single row. That is correct ~99% of the time, but a clinician
-- might legitimately record two distinct events on the same day
-- (e.g. two separate gallstone episodes documented as two notes,
-- two different prescriptions of the same drug at different doses).
-- Those would silently collapse and we would never know — unless we
-- look at the pre-dedup snapshot tables that migration 012 left
-- behind.
--
-- For every grouping that collided on the natural key, this script
-- counts how many DIFFERENT values existed across the columns that
-- the natural_key DID NOT include but that are still clinically
-- meaningful. If any of those distinct counts is > 1, the merge
-- threw away information and is flagged.
--
-- NULL handling: COUNT(DISTINCT col) ignores NULLs, which would hide
-- "one row had a value, the other had NULL" cases — a real loss of
-- information. We coalesce to a sentinel '<NULL>' before the distinct
-- count so null-vs-non-null divergence is also flagged.
--
-- Schema reference (current production):
--   patient_conditions     (id, patient_id, code, display, system,
--                           onset_date, clinical_status, data_source)
--   patient_medications    (id, patient_id, code, display, system,
--                           status, authored_on, data_source)
--   clinical_events        (id, patient_id, event_type, event_date,
--                           description, source_system, data_source)
--   behavioral_screenings  (id, patient_id, instrument_key, domain,
--                           loinc_code, score, band, item_answers,
--                           triggered_critical, source_type,
--                           source_id, administered_at, entered_by,
--                           data_source)
--
-- The task notes "encounter, dose, route" as examples — those columns
-- do NOT exist on these tables in this schema, so we audit on the
-- clinically meaningful fields that DO exist:
--   conditions   → clinical_status, display, system
--   medications  → status, display, system
--   encounters   → description (already in natural_key — we still
--                  surface where source_system or data_source disagreed)
--   screenings   → score, band, item_answers, triggered_critical
--
-- Read-only: SELECT statements only. Safe to run on production.
--
-- Usage:
--   psql "$DATABASE_URL" -f scripts/audits/audit_dedup_collisions.sql

\echo '======================================================================'
\echo 'patient_conditions: same patient + code + date with differing fields'
\echo '======================================================================'
SELECT
    patient_id,
    COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))) AS code_or_hash,
    onset_date,
    COUNT(*)                                                AS row_count,
    COUNT(DISTINCT COALESCE(clinical_status, '<NULL>'))     AS distinct_clinical_status,
    COUNT(DISTINCT COALESCE(display,         '<NULL>'))     AS distinct_display,
    COUNT(DISTINCT COALESCE(system,          '<NULL>'))     AS distinct_system,
    array_agg(DISTINCT clinical_status)                     AS clinical_statuses,
    array_agg(DISTINCT display)                             AS displays
  FROM _pre_012_conditions_backup
 GROUP BY patient_id,
          COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
          onset_date
HAVING COUNT(*) > 1
   AND (COUNT(DISTINCT COALESCE(clinical_status, '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(display,     '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(system,      '<NULL>')) > 1)
 ORDER BY row_count DESC, patient_id;

\echo '======================================================================'
\echo 'patient_medications: same patient + code + authored_on with differing fields'
\echo '======================================================================'
SELECT
    patient_id,
    COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))) AS code_or_hash,
    authored_on,
    COUNT(*)                                                AS row_count,
    COUNT(DISTINCT COALESCE(status,  '<NULL>'))             AS distinct_status,
    COUNT(DISTINCT COALESCE(display, '<NULL>'))             AS distinct_display,
    COUNT(DISTINCT COALESCE(system,  '<NULL>'))             AS distinct_system,
    array_agg(DISTINCT status)                              AS statuses,
    array_agg(DISTINCT display)                             AS displays
  FROM _pre_012_medications_backup
 GROUP BY patient_id,
          COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
          authored_on
HAVING COUNT(*) > 1
   AND (COUNT(DISTINCT COALESCE(status,  '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(display, '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(system,  '<NULL>')) > 1)
 ORDER BY row_count DESC, patient_id;

\echo '======================================================================'
\echo 'clinical_events: collisions where source_system / data_source disagreed'
\echo '======================================================================'
-- description IS part of the natural_key, so groups here have the same
-- description; we surface cases where source_system or data_source
-- differ — these are usually benign, but worth a glance.
SELECT
    patient_id,
    COALESCE(NULLIF(event_type, ''), 'NOTYPE') AS event_type,
    event_date,
    COUNT(*)                                                AS row_count,
    COUNT(DISTINCT COALESCE(source_system, '<NULL>'))       AS distinct_source_system,
    COUNT(DISTINCT COALESCE(data_source,   '<NULL>'))       AS distinct_data_source,
    array_agg(DISTINCT source_system)                       AS source_systems,
    array_agg(DISTINCT data_source)                         AS data_sources
  FROM _pre_012_clinical_events_backup
 GROUP BY patient_id,
          COALESCE(NULLIF(event_type, ''), 'NOTYPE'),
          event_date,
          md5(COALESCE(description, ''))
HAVING COUNT(*) > 1
   AND (COUNT(DISTINCT COALESCE(source_system, '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(data_source,   '<NULL>')) > 1)
 ORDER BY row_count DESC, patient_id;

\echo '======================================================================'
\echo 'behavioral_screenings: same patient+instrument+time with differing answers'
\echo '======================================================================'
SELECT
    patient_id,
    instrument_key,
    administered_at,
    COUNT(*)                                                AS row_count,
    COUNT(DISTINCT COALESCE(score::text,                '<NULL>')) AS distinct_score,
    COUNT(DISTINCT COALESCE(band,                       '<NULL>')) AS distinct_band,
    COUNT(DISTINCT COALESCE(item_answers::text,         '<NULL>')) AS distinct_item_answers,
    COUNT(DISTINCT COALESCE(triggered_critical::text,   '<NULL>')) AS distinct_triggered,
    array_agg(DISTINCT score)                               AS scores,
    array_agg(DISTINCT band)                                AS bands
  FROM _pre_012_behavioral_screenings_backup
 GROUP BY patient_id, instrument_key, administered_at
HAVING COUNT(*) > 1
   AND (COUNT(DISTINCT COALESCE(score::text,              '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(band,                  '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(item_answers::text,    '<NULL>')) > 1
        OR COUNT(DISTINCT COALESCE(triggered_critical::text, '<NULL>')) > 1)
 ORDER BY row_count DESC, patient_id;

\echo '======================================================================'
\echo 'Summary counts'
\echo '======================================================================'
SELECT 'conditions_collisions_total' AS metric,
       COUNT(*) AS value
  FROM (
    SELECT 1
      FROM _pre_012_conditions_backup
     GROUP BY patient_id,
              COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
              onset_date
    HAVING COUNT(*) > 1
  ) g
UNION ALL
SELECT 'conditions_collisions_with_diffs',
       COUNT(*)
  FROM (
    SELECT 1
      FROM _pre_012_conditions_backup
     GROUP BY patient_id,
              COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
              onset_date
    HAVING COUNT(*) > 1
       AND (COUNT(DISTINCT COALESCE(clinical_status, '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(display,     '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(system,      '<NULL>')) > 1)
  ) g
UNION ALL
SELECT 'medications_collisions_total',
       COUNT(*)
  FROM (
    SELECT 1
      FROM _pre_012_medications_backup
     GROUP BY patient_id,
              COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
              authored_on
    HAVING COUNT(*) > 1
  ) g
UNION ALL
SELECT 'medications_collisions_with_diffs',
       COUNT(*)
  FROM (
    SELECT 1
      FROM _pre_012_medications_backup
     GROUP BY patient_id,
              COALESCE(NULLIF(code, ''), 'HASH:' || md5(COALESCE(display, ''))),
              authored_on
    HAVING COUNT(*) > 1
       AND (COUNT(DISTINCT COALESCE(status,  '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(display, '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(system,  '<NULL>')) > 1)
  ) g
UNION ALL
SELECT 'clinical_events_collisions_total',
       COUNT(*)
  FROM (
    SELECT 1
      FROM _pre_012_clinical_events_backup
     GROUP BY patient_id,
              COALESCE(NULLIF(event_type, ''), 'NOTYPE'),
              event_date,
              md5(COALESCE(description, ''))
    HAVING COUNT(*) > 1
  ) g
UNION ALL
SELECT 'clinical_events_collisions_with_diffs',
       COUNT(*)
  FROM (
    SELECT 1
      FROM _pre_012_clinical_events_backup
     GROUP BY patient_id,
              COALESCE(NULLIF(event_type, ''), 'NOTYPE'),
              event_date,
              md5(COALESCE(description, ''))
    HAVING COUNT(*) > 1
       AND (COUNT(DISTINCT COALESCE(source_system, '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(data_source,   '<NULL>')) > 1)
  ) g
UNION ALL
SELECT 'behavioral_screenings_collisions_total',
       COUNT(*)
  FROM (
    SELECT 1
      FROM _pre_012_behavioral_screenings_backup
     GROUP BY patient_id, instrument_key, administered_at
    HAVING COUNT(*) > 1
  ) g
UNION ALL
SELECT 'behavioral_screenings_collisions_with_diffs',
       COUNT(*)
  FROM (
    SELECT 1
      FROM _pre_012_behavioral_screenings_backup
     GROUP BY patient_id, instrument_key, administered_at
    HAVING COUNT(*) > 1
       AND (COUNT(DISTINCT COALESCE(score::text,              '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(band,                  '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(item_answers::text,    '<NULL>')) > 1
            OR COUNT(DISTINCT COALESCE(triggered_critical::text, '<NULL>')) > 1)
  ) g;
