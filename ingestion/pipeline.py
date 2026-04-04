"""Ingestion pipeline: 8-stage data ingestion from external sources to warehouse.

Stages:
  1. Adapter selection (read DATA_TRACK)
  2. Freshness check (query source_freshness)
  3. Raw retrieval (adapter.fetch → raw FHIR JSON)
  4. Cache raw bundle (INSERT INTO raw_fhir_cache)
  5. Normalization (fhir_to_schema → flat DB records)
  6. Conflict resolution (patient-reported > device > healthex > synthea)
  7. Warehouse write (INSERT ... ON CONFLICT DO UPDATE)
  8. Update freshness (UPDATE source_freshness)
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

# Allow importing transforms from mcp-server/
_mcp_server_dir = os.path.join(os.path.dirname(__file__), "..", "mcp-server")
if _mcp_server_dir not in sys.path:
    sys.path.insert(0, _mcp_server_dir)

from ingestion.adapters.synthea import SyntheaAdapter
from ingestion.conflict_resolver import ConflictResolver

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger(__name__)


@dataclass
class IngestionResult:
    """Result of a single ingestion pipeline run."""

    status: str  # completed | failed | skipped_fresh | partial
    records_upserted: int = 0
    conflicts_detected: int = 0
    duration_ms: int = 0
    error_message: str | None = None


class IngestionPipeline:
    """Eight-stage ingestion pipeline."""

    def __init__(self, adapter_name: str = "synthea", pool=None):
        self.adapter_name = adapter_name
        self.pool = pool
        self.resolver = ConflictResolver()

    def _get_adapter(self):
        """Stage 1: Adapter selection based on adapter_name."""
        if self.adapter_name == "synthea":
            return SyntheaAdapter()
        raise ValueError(f"Unknown adapter: {self.adapter_name}")

    async def _check_freshness(self, patient_id: str, conn) -> bool:
        """Stage 2: Check if data is stale and needs refresh."""
        row = await conn.fetchrow(
            """
            SELECT last_ingested_at, ttl_hours
            FROM source_freshness
            WHERE patient_id = $1 AND source_name = $2
            """,
            patient_id,
            self.adapter_name,
        )
        if row is None:
            return True  # No record means never ingested — needs refresh

        last_ingested = row["last_ingested_at"]
        ttl_hours = row["ttl_hours"] or 24
        if last_ingested is None:
            return True

        elapsed_hours = (datetime.utcnow() - last_ingested.replace(tzinfo=None)).total_seconds() / 3600
        return elapsed_hours >= ttl_hours

    async def _cache_raw_bundle(
        self, patient_id: str, bundle: dict[str, Any], conn
    ) -> None:
        """Stage 4: Cache raw FHIR bundle before transformation."""
        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            resource_type = resource.get("resourceType", "Unknown")
            fhir_resource_id = resource.get("id", str(uuid.uuid4()))

            await conn.execute(
                """
                INSERT INTO raw_fhir_cache
                    (id, patient_id, source_name, resource_type,
                     raw_json, fhir_resource_id, retrieved_at, processed, data_source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                ON CONFLICT (patient_id, source_name, fhir_resource_id) DO UPDATE SET
                    raw_json = EXCLUDED.raw_json,
                    retrieved_at = EXCLUDED.retrieved_at,
                    processed = false
                """,
                str(uuid.uuid4()),
                patient_id,
                self.adapter_name,
                resource_type,
                json.dumps(resource),
                fhir_resource_id,
                datetime.utcnow(),
                False,
                self.adapter_name,
            )

    async def _write_to_warehouse(self, records: list[dict], conn) -> int:
        """Stage 7: Write resolved records to warehouse tables.

        Determines the target table from the record fields and uses
        parameterized INSERT ... ON CONFLICT for idempotent writes.
        """
        written = 0
        for rec in records:
            # Remove metadata keys before writing
            table = rec.pop("_table", None)
            rec.pop("_conflict_key", None)

            try:
                if "metric_type" in rec:
                    # biometric_readings
                    await conn.execute(
                        """
                        INSERT INTO biometric_readings
                            (id, patient_id, metric_type, value, unit,
                             measured_at, data_source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT DO NOTHING
                        """,
                        rec.get("id", str(uuid.uuid4())),
                        rec.get("patient_id", ""),
                        rec.get("metric_type", ""),
                        rec.get("value", 0),
                        rec.get("unit", ""),
                        rec.get("measured_at"),
                        rec.get("data_source", self.adapter_name),
                    )
                elif "clinical_status" in rec and "onset_date" in rec:
                    # patient_conditions
                    await conn.execute(
                        """
                        INSERT INTO patient_conditions
                            (id, patient_id, code, display, system,
                             onset_date, clinical_status, data_source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT DO NOTHING
                        """,
                        rec.get("id", str(uuid.uuid4())),
                        rec.get("patient_id", ""),
                        rec.get("code", ""),
                        rec.get("display", ""),
                        rec.get("system", ""),
                        rec.get("onset_date"),
                        rec.get("clinical_status", "active"),
                        rec.get("data_source", self.adapter_name),
                    )
                elif "authored_on" in rec and "status" in rec:
                    # patient_medications
                    await conn.execute(
                        """
                        INSERT INTO patient_medications
                            (id, patient_id, code, display, system,
                             status, authored_on, data_source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                        ON CONFLICT DO NOTHING
                        """,
                        rec.get("id", str(uuid.uuid4())),
                        rec.get("patient_id", ""),
                        rec.get("code", ""),
                        rec.get("display", ""),
                        rec.get("system", ""),
                        rec.get("status", "active"),
                        rec.get("authored_on"),
                        rec.get("data_source", self.adapter_name),
                    )
                elif "event_type" in rec:
                    # clinical_events
                    await conn.execute(
                        """
                        INSERT INTO clinical_events
                            (id, patient_id, event_type, event_date,
                             description, source_system, data_source)
                        VALUES ($1, $2, $3, $4, $5, $6, $7)
                        ON CONFLICT DO NOTHING
                        """,
                        rec.get("id", str(uuid.uuid4())),
                        rec.get("patient_id", ""),
                        rec.get("event_type", ""),
                        rec.get("event_date"),
                        rec.get("description", ""),
                        rec.get("source_system", ""),
                        rec.get("data_source", self.adapter_name),
                    )
                elif "mrn" in rec:
                    # patients
                    await conn.execute(
                        """
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
                        """,
                        rec.get("id", str(uuid.uuid4())),
                        rec.get("mrn", ""),
                        rec.get("first_name", ""),
                        rec.get("last_name", ""),
                        rec.get("birth_date"),
                        rec.get("gender", ""),
                        rec.get("race", ""),
                        rec.get("ethnicity", ""),
                        rec.get("address_line", ""),
                        rec.get("city", ""),
                        rec.get("state", ""),
                        rec.get("zip_code", ""),
                        rec.get("is_synthetic", True),
                        rec.get("data_source", self.adapter_name),
                    )
                else:
                    logger.warning("Unrecognized record shape, skipping: %s", list(rec.keys()))
                    continue
                written += 1
            except Exception as e:
                logger.error("Failed to write record: %s", e)

        return written

    async def _update_freshness(self, patient_id: str, records_count: int, conn) -> None:
        """Stage 8: Update source_freshness after successful ingestion."""
        await conn.execute(
            """
            INSERT INTO source_freshness
                (id, patient_id, source_name, last_ingested_at, records_count,
                 ttl_hours, data_source)
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (patient_id, source_name) DO UPDATE SET
                last_ingested_at = EXCLUDED.last_ingested_at,
                records_count = EXCLUDED.records_count
            """,
            str(uuid.uuid4()),
            patient_id,
            self.adapter_name,
            datetime.utcnow(),
            records_count,
            24 if self.adapter_name == "healthex" else 8760,  # synthea: never (1yr)
            self.adapter_name,
        )

    async def _log_ingestion(
        self, patient_id: str, result: IngestionResult, triggered_by: str, conn
    ) -> None:
        """Log the ingestion run to ingestion_log."""
        await conn.execute(
            """
            INSERT INTO ingestion_log
                (id, patient_id, source_name, status, records_upserted,
                 conflicts_detected, duration_ms, error_message,
                 triggered_by, started_at, data_source)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
            """,
            str(uuid.uuid4()),
            patient_id,
            self.adapter_name,
            result.status,
            result.records_upserted,
            result.conflicts_detected,
            result.duration_ms,
            result.error_message,
            triggered_by,
            datetime.utcnow(),
            self.adapter_name,
        )

    async def run(
        self,
        patient_id: str,
        force_refresh: bool = False,
        triggered_by: str = "schedule",
    ) -> IngestionResult:
        """Execute the full 8-stage ingestion pipeline for a patient.

        Args:
            patient_id: UUID of the patient to ingest data for.
            force_refresh: If True, skip freshness check and always ingest.
            triggered_by: What triggered this run (schedule | pre_visit | force_refresh).

        Returns:
            IngestionResult with status, counts, and timing.
        """
        start_time = time.monotonic()

        try:
            # Stage 1: Adapter selection
            adapter = self._get_adapter()

            async with self.pool.acquire() as conn:
                # Stage 2: Freshness check
                if not force_refresh:
                    is_stale = await self._check_freshness(patient_id, conn)
                    if not is_stale:
                        result = IngestionResult(
                            status="skipped_fresh",
                            duration_ms=int((time.monotonic() - start_time) * 1000),
                        )
                        await self._log_ingestion(patient_id, result, triggered_by, conn)
                        return result

                # Stage 3: Raw retrieval
                patients = await adapter.load_all_patients()
                target_bundle = None
                for p in patients:
                    if p.patient_ref_id == patient_id:
                        target_bundle = p.fhir_bundle
                        break

                if target_bundle is None:
                    # No bundle found for this patient — may already be in DB
                    result = IngestionResult(
                        status="completed",
                        records_upserted=0,
                        duration_ms=int((time.monotonic() - start_time) * 1000),
                    )
                    await self._log_ingestion(patient_id, result, triggered_by, conn)
                    await self._update_freshness(patient_id, 0, conn)
                    return result

                # Stage 4: Cache raw bundle
                await self._cache_raw_bundle(patient_id, target_bundle, conn)

                # Stage 5: Normalization — transform FHIR to DB records
                from transforms.fhir_to_schema import transform_by_type

                # Map standard FHIR resourceTypes to transform_by_type keys
                fhir_type_map = {
                    "Patient": "summary",
                    "Condition": "conditions",
                    "MedicationRequest": "medications",
                    "Observation": "labs",
                    "Encounter": "encounters",
                }

                # Group resources by type
                resources_by_type: dict[str, list] = {}
                for entry in target_bundle.get("entry", []):
                    resource = entry.get("resource", {})
                    rt = resource.get("resourceType", "")
                    resources_by_type.setdefault(rt, []).append(resource)

                records: list[dict] = []
                for fhir_rt, resources in resources_by_type.items():
                    mapped_type = fhir_type_map.get(fhir_rt)
                    if not mapped_type:
                        continue
                    try:
                        records.extend(
                            transform_by_type(
                                mapped_type, resources, patient_id,
                                source=self.adapter_name,
                            )
                        )
                    except ValueError:
                        pass  # unknown resource type — skip

                # Stage 6: Conflict resolution
                resolved = ConflictResolver.apply(records, policy="patient_first")

                # Stage 7: Warehouse write
                records_upserted = await self._write_to_warehouse(resolved, conn)

                # Stage 8: Update freshness
                await self._update_freshness(patient_id, records_upserted, conn)

                duration_ms = int((time.monotonic() - start_time) * 1000)
                result = IngestionResult(
                    status="completed",
                    records_upserted=records_upserted,
                    conflicts_detected=0,
                    duration_ms=duration_ms,
                )
                await self._log_ingestion(patient_id, result, triggered_by, conn)
                return result

        except Exception as e:
            duration_ms = int((time.monotonic() - start_time) * 1000)
            result = IngestionResult(
                status="failed",
                duration_ms=duration_ms,
                error_message=str(e),
            )
            try:
                async with self.pool.acquire() as conn:
                    await self._log_ingestion(patient_id, result, triggered_by, conn)
            except Exception:
                logger.error("Failed to log ingestion error: %s", e)
            return result
