"""
PATCH 2 — Fix transform_patient: add is_synthetic parameter
File: mcp-server/transforms/fhir_to_schema.py

Apply as a search-and-replace. Two hunks.
"""

# ── HUNK 2a: function signature ──────────────────────────────────────────────
# FIND (exact):
"""
def transform_patient(
    patient_resource: dict[str, Any],
    data_source: str = "synthea",
) -> dict[str, Any]:
"""

# REPLACE WITH:
"""
def transform_patient(
    patient_resource: dict[str, Any],
    data_source: str = "synthea",
    is_synthetic: bool = True,
) -> dict[str, Any]:
"""

# ── HUNK 2b: return dict ─────────────────────────────────────────────────────
# FIND (exact, inside the return statement near line 85):
"""
        "is_synthetic": True,
"""

# REPLACE WITH:
"""
        "is_synthetic": is_synthetic,
"""

# ── No other changes needed in fhir_to_schema.py ────────────────────────────
# All callers that don't pass is_synthetic still get True (Synthea behaviour
# preserved). Only register_healthex_patient passes is_synthetic=False.


"""
PATCH 3 — Fix ingest_from_healthex: return canonical UUID after summary write
File: mcp-server/skills/ingestion_tools.py

Inside the ingest_from_healthex tool, inside the "elif resource_type == 'summary':"
branch, after the conn.execute INSERT, add the fetchrow and track canonical_id.
Then use canonical_id in the final return string.

Apply as two search-and-replace hunks.
"""

# ── HUNK 3a: after the summary INSERT, capture canonical UUID ────────────────
# FIND (exact — the closing of the summary elif branch, around line 218):
"""
                    elif resource_type == "summary":
                        await conn.execute(
                            \"\"\"
                            INSERT INTO patients
                                (id, mrn, first_name, last_name, birth_date,
                                 gender, race, ethnicity, address_line, city,
                                 state, zip_code, is_synthetic, data_source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                                    $11, $12, $13, $14)
                            ON CONFLICT (mrn) DO UPDATE SET
                                first_name = EXCLUDED.first_name,
                                last_name = EXCLUDED.last_name,
                                birth_date = EXCLUDED.birth_date,
                                gender = EXCLUDED.gender,
                                race = EXCLUDED.race,
                                ethnicity = EXCLUDED.ethnicity,
                                data_source = EXCLUDED.data_source
                            \"\"\",
                            rec.get(\"id\", str(uuid.uuid4())),
                            rec.get(\"mrn\", \"\"),
                            rec.get(\"first_name\", \"\"),
                            rec.get(\"last_name\", \"\"),
                            rec.get(\"birth_date\"),
                            rec.get(\"gender\", \"\"),
                            rec.get(\"race\", \"\"),
                            rec.get(\"ethnicity\", \"\"),
                            rec.get(\"address_line\", \"\"),
                            rec.get(\"city\", \"\"),
                            rec.get(\"state\", \"\"),
                            rec.get(\"zip_code\", \"\"),
                            rec.get(\"is_synthetic\", False),
                            rec.get(\"data_source\", \"healthex\"),
                        )
                    records_written += 1
"""

# REPLACE WITH:
"""
                    elif resource_type == "summary":
                        await conn.execute(
                            \"\"\"
                            INSERT INTO patients
                                (id, mrn, first_name, last_name, birth_date,
                                 gender, race, ethnicity, address_line, city,
                                 state, zip_code, is_synthetic, data_source)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10,
                                    $11, $12, $13, $14)
                            ON CONFLICT (mrn) DO UPDATE SET
                                first_name = EXCLUDED.first_name,
                                last_name = EXCLUDED.last_name,
                                birth_date = EXCLUDED.birth_date,
                                gender = EXCLUDED.gender,
                                race = EXCLUDED.race,
                                ethnicity = EXCLUDED.ethnicity,
                                data_source = EXCLUDED.data_source
                            \"\"\",
                            rec.get(\"id\", str(uuid.uuid4())),
                            rec.get(\"mrn\", \"\"),
                            rec.get(\"first_name\", \"\"),
                            rec.get(\"last_name\", \"\"),
                            rec.get(\"birth_date\"),
                            rec.get(\"gender\", \"\"),
                            rec.get(\"race\", \"\"),
                            rec.get(\"ethnicity\", \"\"),
                            rec.get(\"address_line\", \"\"),
                            rec.get(\"city\", \"\"),
                            rec.get(\"state\", \"\"),
                            rec.get(\"zip_code\", \"\"),
                            rec.get(\"is_synthetic\", False),
                            rec.get(\"data_source\", \"healthex\"),
                        )
                        # Retrieve the UUID that survived ON CONFLICT (mrn)
                        # so callers that skipped register_healthex_patient
                        # can recover the correct patient_id from the return value.
                        _summary_row = await conn.fetchrow(
                            \"SELECT id FROM patients WHERE mrn = $1\",
                            rec.get(\"mrn\", \"\"),
                        )
                        if _summary_row:
                            patient_id = str(_summary_row[\"id\"])
                    records_written += 1
"""

# ── HUNK 3b: use patient_id (now canonical) in return string ─────────────────
# FIND (exact — the final return at the end of ingest_from_healthex):
"""
            return (
                f"OK HealthEx {resource_type} ingested | "
                f"{records_written} records written | "
                f"{duration_ms}ms | patient={patient_id}"
            )
"""

# REPLACE WITH:
"""
            return json.dumps({
                "status": "ok",
                "resource_type": resource_type,
                "records_written": records_written,
                "duration_ms": duration_ms,
                "patient_id": patient_id,
            })
"""
# NOTE: returning JSON instead of a plain string makes it easier for the
# orchestrating Claude session to extract patient_id programmatically when
# register_healthex_patient was accidentally skipped.
