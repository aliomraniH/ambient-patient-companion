"""
PATCH 1 — New tool: register_healthex_patient
File: mcp-server/skills/ingestion_tools.py

Insert this block inside the register(mcp: FastMCP) function,
BEFORE the existing ingest_from_healthex tool definition.

This is the bootstrapper that was missing from the session-bridge protocol.
It must be called once per HealthEx session (before any ingest_from_healthex
calls) to ensure the patient has a row in the warehouse.
"""

    @mcp.tool
    async def register_healthex_patient(
        health_summary_json: str,
        mrn_override: str = "",
    ) -> str:
        """Register a real HealthEx patient in the warehouse.

        MUST be called before any ingest_from_healthex calls in a HealthEx
        session. Takes the raw JSON from HealthEx get_health_summary, creates
        or finds the patient row with is_synthetic=False, initialises
        data_sources and source_freshness rows, and returns the canonical
        patient_id UUID and MRN for all subsequent calls.

        Also sets DATA_TRACK = "healthex" in system_config so all future
        pipeline runs use the HealthEx adapter.

        Args:
            health_summary_json: Raw JSON string from HealthEx get_health_summary.
                                 May be a FHIR Patient resource, a FHIR Bundle,
                                 or a HealthEx summary dict — all are handled.
            mrn_override:        If provided, use this MRN instead of extracting
                                 from the summary. Useful when HealthEx summary
                                 does not include an MRN identifier.

        Returns:
            JSON string with patient_id, mrn, and status.
            Example: {"status": "registered", "patient_id": "uuid...", "mrn": "HX-ABC123"}
        """
        import time
        pool = await get_pool()
        try:
            start = time.time()
            summary = json.loads(health_summary_json)

            # ── 1. Extract a FHIR Patient resource from whatever shape arrived ──
            patient_resource: dict = {}

            if summary.get("resourceType") == "Patient":
                # Already a bare FHIR Patient resource
                patient_resource = summary

            elif summary.get("resourceType") == "Bundle":
                # FHIR Bundle — pull the first Patient entry
                for entry in summary.get("entry", []):
                    res = entry.get("resource", {})
                    if res.get("resourceType") == "Patient":
                        patient_resource = res
                        break
                if not patient_resource:
                    return (
                        'Error: FHIR Bundle contained no Patient resource. '
                        'Pass the raw get_health_summary JSON directly.'
                    )

            else:
                # HealthEx summary dict — build a minimal synthetic FHIR Patient
                # so we can reuse transform_patient without a code fork.
                name_parts = summary.get("name", summary.get("full_name", ""))
                if isinstance(name_parts, str):
                    parts = name_parts.strip().split()
                    given = parts[:-1] if len(parts) > 1 else parts
                    family = parts[-1] if len(parts) > 1 else ""
                else:
                    given = [name_parts.get("first", "")]
                    family = name_parts.get("last", "")

                patient_resource = {
                    "resourceType": "Patient",
                    "id": summary.get("id", ""),
                    "name": [{"given": given, "family": family}],
                    "birthDate": summary.get(
                        "birth_date",
                        summary.get("dob", summary.get("date_of_birth", "")),
                    ),
                    "gender": summary.get("gender", summary.get("sex", "")),
                    "identifier": [],
                    "address": [
                        {
                            "line": [summary.get("address", "")],
                            "city": summary.get("city", ""),
                            "state": summary.get("state", ""),
                            "postalCode": summary.get(
                                "zip", summary.get("zip_code", "")
                            ),
                        }
                    ],
                }
                # Inject MRN if the summary has one
                raw_mrn = (
                    mrn_override
                    or summary.get("mrn")
                    or summary.get("patient_id")
                    or summary.get("id")
                    or ""
                )
                if raw_mrn:
                    patient_resource["identifier"] = [
                        {
                            "type": {"coding": [{"code": "MR"}]},
                            "value": str(raw_mrn),
                        }
                    ]

            # ── 2. Transform to patients-table schema ──
            from transforms.fhir_to_schema import transform_patient

            demo = transform_patient(
                patient_resource,
                data_source="healthex",
                is_synthetic=False,
            )

            # Apply MRN override if provided
            if mrn_override:
                demo["mrn"] = mrn_override

            # Ensure we always have a usable MRN
            if not demo.get("mrn"):
                demo["mrn"] = f"HX-{uuid.uuid4().hex[:8].upper()}"
                logger.warning(
                    "register_healthex_patient: no MRN found in summary, "
                    "generated: %s", demo["mrn"]
                )

            # ── 3. Upsert patient row ──
            new_id = str(uuid.uuid4())

            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO patients
                        (id, mrn, first_name, last_name, birth_date, gender,
                         race, ethnicity, address_line, city, state, zip_code,
                         is_synthetic, created_at, data_source)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15)
                    ON CONFLICT (mrn) DO UPDATE SET
                        first_name   = EXCLUDED.first_name,
                        last_name    = EXCLUDED.last_name,
                        birth_date   = EXCLUDED.birth_date,
                        gender       = EXCLUDED.gender,
                        race         = EXCLUDED.race,
                        ethnicity    = EXCLUDED.ethnicity,
                        address_line = EXCLUDED.address_line,
                        city         = EXCLUDED.city,
                        state        = EXCLUDED.state,
                        zip_code     = EXCLUDED.zip_code,
                        is_synthetic = false,
                        data_source  = 'healthex'
                    """,
                    new_id,
                    demo["mrn"],
                    demo.get("first_name", ""),
                    demo.get("last_name", ""),
                    demo.get("birth_date"),         # date | None — asyncpg handles it
                    demo.get("gender", ""),
                    demo.get("race", ""),
                    demo.get("ethnicity", ""),
                    demo.get("address_line", ""),
                    demo.get("city", ""),
                    demo.get("state", ""),
                    demo.get("zip_code", ""),
                    False,                          # is_synthetic
                    datetime.utcnow(),
                    "healthex",
                )

                # Retrieve the canonical UUID (survives ON CONFLICT)
                row = await conn.fetchrow(
                    "SELECT id FROM patients WHERE mrn = $1", demo["mrn"]
                )
                patient_id = str(row["id"]) if row else new_id

                # ── 4. Ensure data_sources row exists ──
                await conn.execute(
                    """
                    INSERT INTO data_sources
                        (id, patient_id, source_name, is_active, connected_at, data_source)
                    VALUES ($1, $2, $3, $4, $5, $6)
                    ON CONFLICT (patient_id, source_name) DO UPDATE SET
                        is_active    = true,
                        connected_at = NOW()
                    """,
                    str(uuid.uuid4()),
                    patient_id,
                    "healthex",
                    True,
                    datetime.utcnow(),
                    "healthex",
                )

                # ── 5. Initialise source_freshness row ──
                await conn.execute(
                    """
                    INSERT INTO source_freshness
                        (patient_id, source_name, last_ingested_at,
                         records_count, ttl_hours)
                    VALUES ($1, $2, NOW(), 0, 24)
                    ON CONFLICT (patient_id, source_name) DO NOTHING
                    """,
                    patient_id,
                    "healthex",
                )

                # ── 6. Switch DATA_TRACK to healthex ──
                await conn.execute(
                    """
                    INSERT INTO system_config (key, value, updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()
                    """,
                    "DATA_TRACK",
                    "healthex",
                )

                await log_skill_execution(
                    conn,
                    "register_healthex_patient",
                    patient_id,
                    "completed",
                    output_data={
                        "mrn": demo["mrn"],
                        "patient_id": patient_id,
                        "duration_ms": int((time.time() - start) * 1000),
                    },
                    data_source="healthex",
                )

            return json.dumps(
                {
                    "status": "registered",
                    "patient_id": patient_id,
                    "mrn": demo["mrn"],
                    "name": f"{demo.get('first_name','')} {demo.get('last_name','')}".strip(),
                    "is_synthetic": False,
                    "data_track": "healthex",
                    "next_step": (
                        f"Call ingest_from_healthex(patient_id='{patient_id}', "
                        "resource_type='labs'|'medications'|'conditions'|'encounters', "
                        "fhir_json=<HealthEx response>) for each resource type, "
                        f"then run_deliberation(patient_id='{patient_id}')."
                    ),
                },
                indent=2,
            )

        except json.JSONDecodeError as e:
            msg = f"Error: health_summary_json is not valid JSON — {e}"
            logger.error("register_healthex_patient: %s", msg)
            return msg
        except Exception as e:
            logger.error("register_healthex_patient failed: %s", e)
            try:
                async with pool.acquire() as conn:
                    await log_skill_execution(
                        conn,
                        "register_healthex_patient",
                        None,
                        "failed",
                        error_message=str(e),
                        data_source="healthex",
                    )
            except Exception:
                logger.error("Failed to log register_healthex_patient error")
            return f"Error: {e}"
