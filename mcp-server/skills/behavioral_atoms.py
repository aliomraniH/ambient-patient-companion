"""
behavioral_atoms.py — MCP tools for behavioral signal atom management.

Tools (registered via register(mcp)):
  - extract_and_store_behavioral_atoms
  - get_behavioral_atom_pressure
  - search_behavioral_atoms_cohort
  - ingest_behavioral_screening_fhir
  - run_behavioral_gap_detection
  - get_behavioral_gaps
  - refresh_behavioral_pressure_view
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from shared.coercion import coerce_confidence

log = logging.getLogger(__name__)


def register(mcp) -> None:
    """Register all behavioral atom MCP tools."""

    # ── extract_and_store_behavioral_atoms ────────────────────────────────────

    @mcp.tool()
    async def extract_and_store_behavioral_atoms(
        patient_id: str,
        text: str,
        source_type: str = "conversation",
        source_id: Optional[str] = None,
        min_confidence: float = 0.60,
    ) -> dict:
        """Extract behavioral signal atoms from free text and persist them to the DB.

        Runs the rule-based extractor, embeds each atom's signal_value using the
        configured backend (MedCPT > OpenAI > stub), and writes rows to
        behavioral_signal_atoms. Returns a summary of what was stored.

        Args:
            patient_id:     UUID of the patient.
            text:           Free text to analyse (conversation turn, note, etc.).
            source_type:    'conversation'|'clinical_note'|'checkin'.
            source_id:      UUID of the originating row (optional).
            min_confidence: Discard atoms below this confidence (default 0.60).

        Returns:
            {atoms_extracted, atoms_stored, signal_types_found, backend_used}
        """
        from db.connection import get_pool
        from skills.behavioral_atom_extractor import extract_atoms_from_text
        from skills.atom_embedder import embed_signal_value, active_backend

        pool = await get_pool()
        atoms = extract_atoms_from_text(
            text,
            source_type=source_type,
            source_id=source_id,
            min_confidence=min_confidence,
        )

        if not atoms:
            return {
                "atoms_extracted": 0,
                "atoms_stored": 0,
                "signal_types_found": [],
                "backend_used": active_backend(),
            }

        stored = 0
        signal_types_found = set()

        async with pool.acquire() as conn:
            for atom in atoms:
                embedding = embed_signal_value(atom.signal_value)
                embedding_str = (
                    "[" + ",".join(str(x) for x in embedding) + "]"
                    if embedding else None
                )
                try:
                    await conn.execute(
                        """
                        INSERT INTO behavioral_signal_atoms
                            (id, patient_id, signal_type, signal_value, confidence,
                             source_type, source_id, extracted_at, embedding, data_source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,NOW(),$8::vector,$9)
                        ON CONFLICT DO NOTHING
                        """,
                        str(uuid.uuid4()),
                        patient_id,
                        atom.signal_type,
                        atom.signal_value,
                        coerce_confidence(atom.confidence),
                        atom.source_type,
                        atom.source_id,
                        embedding_str,
                        "healthex",
                    )
                    stored += 1
                    signal_types_found.add(atom.signal_type)
                except Exception as e:
                    log.warning("store atom failed: %s", e)

        # Auto-refresh the materialized view so get_behavioral_atom_pressure
        # and run_behavioral_gap_detection see the newly inserted atoms
        # without requiring a separate manual refresh call.
        if stored > 0:
            from skills.atom_vector_search import refresh_atom_pressure_view
            await refresh_atom_pressure_view(pool)

        return {
            "atoms_extracted": len(atoms),
            "atoms_stored": stored,
            "signal_types_found": sorted(signal_types_found),
            "backend_used": active_backend(),
            "pressure_view_refreshed": stored > 0,
        }

    # ── get_behavioral_atom_pressure ──────────────────────────────────────────

    @mcp.tool()
    async def get_behavioral_atom_pressure(
        patient_id: str,
        signal_types: Optional[list[str]] = None,
    ) -> dict:
        """Read the current behavioral atom pressure for a patient.

        Reads from the atom_pressure_scores materialized view (90-day window).
        Returns pressure per signal_type with count and recency.

        Args:
            patient_id:   UUID of the patient.
            signal_types: Filter to specific signal types (optional).

        Returns:
            {patient_id, pressure: {signal_type: {pressure_score, present_atom_count, last_atom_at}}}
        """
        from db.connection import get_pool
        from skills.atom_vector_search import get_atom_pressure_for_patient

        pool = await get_pool()
        pressure = await get_atom_pressure_for_patient(pool, patient_id, signal_types)
        return {"patient_id": patient_id, "pressure": pressure}

    # ── search_behavioral_atoms_cohort ────────────────────────────────────────

    @mcp.tool()
    async def search_behavioral_atoms_cohort(
        query_text: str,
        signal_type: Optional[str] = None,
        top_k: int = 10,
        min_similarity: float = 0.75,
        days_lookback: int = 90,
    ) -> dict:
        """Semantic similarity search over behavioral atoms for the full cohort.

        PRIVACY: Returns aggregated statistics per (patient_id, signal_type) only.
        Never returns raw signal_value text (PHI).

        Args:
            query_text:     Text to embed and search against.
            signal_type:    Optional filter to one signal type.
            top_k:          Max results.
            min_similarity: Cosine similarity floor (default 0.75).
            days_lookback:  Restrict to atoms from the past N days (default 90).

        Returns:
            {results: [{patient_id, signal_type, atom_count, avg_confidence, avg_similarity, last_seen_at}]}
        """
        from db.connection import get_pool
        from skills.atom_embedder import embed_signal_value
        from skills.atom_vector_search import search_similar_atoms

        pool = await get_pool()
        embedding = embed_signal_value(query_text)
        if not embedding:
            return {"results": [], "error": "embedding_failed"}

        results = await search_similar_atoms(
            pool,
            query_embedding=embedding,
            patient_id=None,  # cohort scope
            signal_type=signal_type,
            top_k=top_k,
            min_similarity=min_similarity,
            days_lookback=days_lookback,
        )
        # Ensure datetimes are serializable
        for r in results:
            for k, v in r.items():
                if isinstance(v, datetime):
                    r[k] = v.isoformat()
        return {"results": results}

    # ── ingest_behavioral_screening_fhir ─────────────────────────────────────

    @mcp.tool()
    async def ingest_behavioral_screening_fhir(
        patient_id: str,
        fhir_resource_json: str,
        source_type: str = "fhir_observation",
        source_id: Optional[str] = None,
    ) -> dict:
        """Parse a FHIR Observation or QuestionnaireResponse and write to behavioral_screenings.

        Recognises instruments defined in the SCREENING_REGISTRY by their LOINC code.
        Triggered critical items are detected and returned for downstream escalation.

        Args:
            patient_id:          UUID of the patient.
            fhir_resource_json:  JSON string of the FHIR resource.
            source_type:         'fhir_observation'|'questionnaire_response'.
            source_id:           UUID of the raw_fhir_cache row (optional).

        Returns:
            {inserted, instrument_key, domain, score, band, critical_count, triggered_critical}
        """
        from db.connection import get_pool
        from skills.behavioral_screening_ingestor import ingest_observation_or_qr

        pool = await get_pool()
        try:
            resource = json.loads(fhir_resource_json)
        except json.JSONDecodeError as e:
            return {"inserted": False, "error": f"invalid JSON: {e}"}

        result = await ingest_observation_or_qr(
            pool, patient_id, resource, source_type, source_id,
        )

        if result is None:
            return {"inserted": False, "reason": "loinc_not_in_registry"}

        # Auto-resolve gap if one exists for this domain
        from skills.behavioral_gap_detector import resolve_gap
        if result.get("id"):
            await resolve_gap(pool, patient_id, result["domain"], result["id"])

        return {"inserted": True, **result}

    # ── run_behavioral_gap_detection ──────────────────────────────────────────

    @mcp.tool()
    async def run_behavioral_gap_detection(
        patient_id: str,
        pressure_threshold: float = 0.40,
    ) -> dict:
        """Run the behavioral screening gap detector for a patient.

        Identifies domains where atom pressure exceeds the threshold but no
        qualifying screening has been administered within the lookback window.
        Writes open gaps to behavioral_screening_gaps and upserts phenotype labels.

        Args:
            patient_id:         UUID of the patient.
            pressure_threshold: Atom pressure score above which a gap is triggered.
                                Default 0.40.

        Returns:
            {patient_id, gaps_detected, gaps: [{domain, gap_type, pressure_score,
             temporal_confidence, suggested_instruments, phenotype_label}]}
        """
        from db.connection import get_pool
        from skills.behavioral_gap_detector import run_gap_detector_for_patient

        pool = await get_pool()
        gaps = await run_gap_detector_for_patient(
            pool, patient_id, pressure_threshold=pressure_threshold,
        )
        return {
            "patient_id": patient_id,
            "gaps_detected": len(gaps),
            "gaps": gaps,
        }

    # ── get_behavioral_gaps ───────────────────────────────────────────────────

    @mcp.tool()
    async def get_behavioral_gaps(patient_id: str) -> dict:
        """Retrieve open behavioral screening gaps for a patient.

        Args:
            patient_id: UUID of the patient.

        Returns:
            {patient_id, open_gap_count, gaps: [{domain, gap_type, pressure_score,
             temporal_confidence, suggested_instruments, phenotype_label, triggered_at}]}
        """
        from db.connection import get_pool
        from skills.behavioral_gap_detector import get_open_gaps_for_patient

        pool = await get_pool()
        gaps = await get_open_gaps_for_patient(pool, patient_id)
        for g in gaps:
            for k, v in g.items():
                if isinstance(v, datetime):
                    g[k] = v.isoformat()
        return {
            "patient_id": patient_id,
            "open_gap_count": len(gaps),
            "gaps": gaps,
        }

    # ── refresh_behavioral_pressure_view ─────────────────────────────────────

    @mcp.tool()
    async def refresh_behavioral_pressure_view() -> dict:
        """Refresh the atom_pressure_scores materialized view.

        Should be called after a batch of atoms are inserted or on a schedule.
        Returns success status.
        """
        from db.connection import get_pool
        from skills.atom_vector_search import refresh_atom_pressure_view

        pool = await get_pool()
        ok = await refresh_atom_pressure_view(pool)
        return {"refreshed": ok}

    # ── scan_notes_for_behavioral_screenings ──────────────────────────────────

    @mcp.tool()
    async def scan_notes_for_behavioral_screenings(
        patient_id: str,
        limit: int = 200,
    ) -> dict:
        """Scan clinical_notes for structured screening scores (PHQ-9 etc.) and write to behavioral_screenings.

        Bridges the gap between free-text clinical note corpus and the structured
        behavioral_screenings table. Uses regex patterns to detect PHQ-9 total scores
        and item-9 endorsements, then writes rows via ingest_behavioral_screening_fhir
        logic. Idempotent — ON CONFLICT DO NOTHING skips already-persisted rows.

        Args:
            patient_id: UUID of the patient.
            limit:       Maximum number of notes to scan (default 200).

        Returns:
            {patient_id, notes_scanned, screenings_found, screenings_written,
             found: [{note_id, note_date, instrument, score, item9, inserted}]}
        """
        import re as _re
        import uuid as _uuid_scan
        from datetime import timezone as _tz_scan
        from db.connection import get_pool

        pool = await get_pool()

        # PHQ-9 total score patterns
        _phq9_total = _re.compile(
            r'PHQ[\s\-_]?9[\s\w]*?(?:score|total|result)?[\s:=\-]+(\d{1,2})',
            _re.IGNORECASE,
        )
        # Item 9 (suicidal ideation) patterns
        _item9_numeric = _re.compile(
            r'(?:item\s*[#\-]?\s*9|q(?:uestion)?\s*9)[\s\w]*?(?:scored?|answered?)?[\s:=\-]+([0-3])',
            _re.IGNORECASE,
        )
        _item9_endorsed = _re.compile(
            r'item\s*[#\-]?\s*9\s+(?:was\s+)?endorsed',
            _re.IGNORECASE,
        )

        def _score_to_band(s: int) -> str:
            if s <= 4: return "minimal"
            if s <= 9: return "mild"
            if s <= 14: return "moderate"
            if s <= 19: return "moderately_severe"
            return "severe"

        async with pool.acquire() as conn:
            notes = await conn.fetch(
                """
                SELECT id, note_text, note_type, note_date
                FROM clinical_notes
                WHERE patient_id = $1::uuid
                ORDER BY note_date DESC NULLS LAST
                LIMIT $2
                """,
                patient_id, limit,
            )

            found = []
            screenings_written = 0

            for note in notes:
                note_text = note["note_text"] or ""
                phq9_match = _phq9_total.search(note_text)
                if not phq9_match:
                    continue

                try:
                    phq9_score = int(phq9_match.group(1))
                except ValueError:
                    continue

                item9_match = _item9_numeric.search(note_text)
                item9 = 0
                if item9_match:
                    try:
                        item9 = int(item9_match.group(1))
                    except ValueError:
                        item9 = 0
                elif _item9_endorsed.search(note_text):
                    item9 = 1

                item_answers = {"9": item9}
                triggered_critical = []
                if item9 > 0:
                    triggered_critical.append({
                        "item": "9",
                        "score": item9,
                        "alert_text": (
                            "PHQ-9 item 9 endorsed — suicidal ideation screen positive. "
                            "Safety review required."
                        ),
                        "severity": "passive_si" if item9 == 1 else "active_si",
                    })

                note_date = note["note_date"] or datetime.now(tz=_tz_scan.utc)

                inserted = False
                try:
                    result = await conn.execute(
                        """
                        INSERT INTO behavioral_screenings
                            (id, patient_id, instrument_key, domain, loinc_code,
                             score, band, item_answers, triggered_critical,
                             source_type, administered_at, entered_by, data_source)
                        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10,$11,$12,$13)
                        ON CONFLICT DO NOTHING
                        """,
                        str(_uuid_scan.uuid4()), patient_id,
                        "PHQ-9", "depression", "44249-1",
                        phq9_score, _score_to_band(phq9_score),
                        json.dumps(item_answers), json.dumps(triggered_critical),
                        "clinical_note", note_date,
                        "scan_notes_for_behavioral_screenings", "healthex",
                    )
                    inserted = result == "INSERT 0 1"
                    if inserted:
                        screenings_written += 1
                        log.info(
                            "scan_notes: PHQ-9 score=%d item9=%d extracted for %s",
                            phq9_score, item9, patient_id,
                        )
                except Exception as exc:
                    log.warning("scan_notes: insert failed: %s", exc)

                found.append({
                    "note_id": str(note["id"]),
                    "note_date": note_date.isoformat() if hasattr(note_date, "isoformat") else str(note_date),
                    "instrument": "PHQ-9",
                    "score": phq9_score,
                    "item9": item9,
                    "inserted": inserted,
                })

        return {
            "patient_id": patient_id,
            "notes_scanned": len(notes),
            "screenings_found": len(found),
            "screenings_written": screenings_written,
            "found": found,
        }


async def get_behavioral_context(
    db_pool,
    patient_id: str,
    role: str = "pcp",
) -> Optional[dict]:
    """Return a behavioral context dict for use by the deliberation layer.

    Shape:
        {
          open_gaps:      [ {domain, gap_type, pressure_score, ...}, ... ],
          all_screenings: [ {id, instrument_key, domain, score, band,
                             administered_at, ...}, ... ],
          domain_summary: { domain: {screened, has_open_gap, ...}, ... },
          critical_flags: [ {domain, alert_text, triggered_at}, ... ],
        }

    Returns None when no behavioral data exists or on any fetch error.
    """
    try:
        from skills.behavioral_gap_detector import get_open_gaps_for_patient
        from skills.behavioral_cards import build_cards_from_pool
        from skills.screening_registry import SCREENING_REGISTRY

        open_gaps = await get_open_gaps_for_patient(db_pool, patient_id)

        async with db_pool.acquire() as conn:
            screening_rows = await conn.fetch(
                """
                SELECT id::text, instrument_key, domain, loinc_code, score,
                       band, item_answers, triggered_critical, administered_at
                FROM behavioral_screenings
                WHERE patient_id = $1::uuid
                ORDER BY administered_at DESC
                LIMIT 50
                """,
                patient_id,
            )

        all_screenings = []
        critical_flags = []
        for row in screening_rows:
            s = {
                "id": row["id"],
                "instrument_key": row["instrument_key"],
                "domain": row["domain"],
                "score": row["score"],
                "band": row["band"],
                "administered_at": (
                    row["administered_at"].isoformat()
                    if row["administered_at"] else None
                ),
            }
            all_screenings.append(s)
            tc = row["triggered_critical"]
            if tc:
                import json as _json
                items = _json.loads(tc) if isinstance(tc, str) else tc
                for flag in (items or []):
                    critical_flags.append({
                        "domain": row["domain"],
                        "alert_text": flag.get("alert_text", ""),
                        "administered_at": s["administered_at"],
                    })

        gap_domains = {g["domain"] for g in open_gaps}
        screened_domains = {}
        for s in all_screenings:
            d = s["domain"]
            if d not in screened_domains:
                screened_domains[d] = s

        domain_summary = {}
        for key in SCREENING_REGISTRY:
            inst = SCREENING_REGISTRY[key]
            d = inst.domain
            if d not in domain_summary:
                last = screened_domains.get(d)
                domain_summary[d] = {
                    "label": d.replace("_", " ").title(),
                    "screened": d in screened_domains,
                    "last_screened": last["administered_at"] if last else None,
                    "has_open_gap": d in gap_domains,
                }

        return {
            "open_gaps": open_gaps,
            "all_screenings": all_screenings,
            "domain_summary": domain_summary,
            "critical_flags": critical_flags,
        }
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "get_behavioral_context failed: %s", type(e).__name__
        )
        return None


async def run_behavioral_gap_check(
    db_pool,
    patient_id: str,
) -> list[dict]:
    """Run gap detection for a patient. Returns list of detected domain gaps.

    Convenience alias for the deliberation and executor layers.
    Always returns a list (empty if no gaps or on failure).
    """
    try:
        from skills.behavioral_gap_detector import run_gap_detector_for_patient
        return await run_gap_detector_for_patient(db_pool, patient_id)
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(
            "run_behavioral_gap_check failed: %s", type(e).__name__
        )
        return []
