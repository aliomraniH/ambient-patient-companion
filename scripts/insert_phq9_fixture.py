#!/usr/bin/env python3
"""Insert a PHQ-9 test fixture for Maria Chen with a positive item 9 (passive SI)
dated December 2023 — to verify the crisis_risk fix in compute_provider_risk."""

import asyncio
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import asyncpg

PATIENT_ID = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

ITEM_ANSWERS = {
    "1": 2,  # Little interest or pleasure
    "2": 2,  # Feeling down, depressed, hopeless
    "3": 1,  # Trouble sleeping
    "4": 1,  # Feeling tired
    "5": 1,  # Poor appetite
    "6": 0,  # Feeling bad about yourself
    "7": 1,  # Trouble concentrating
    "8": 1,  # Moving slowly / fidgety
    "9": 1,  # *** Passive suicidal ideation (several days) ***
}

# triggered_critical is a JSONB ARRAY per the migration:
#   idx_bs_critical uses jsonb_array_length(triggered_critical) > 0
#   default is '[]' — so this must be a list of alert objects, not a dict
TRIGGERED_CRITICAL = [
    {
        "item_number": 9,
        "item_key": "phq9_item9",
        "alert_text": "Passive suicidal ideation — PHQ-9 item 9 score 1 (several days)",
        "actual_score": 1,
        "threshold": 0,
        "severity": "critical",
        "tag": "passive_suicidal_ideation",
    }
]

ADMINISTERED_AT = datetime(2023, 12, 14, 9, 30, 0, tzinfo=timezone.utc)


async def main():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])

    # asyncpg needs a JSON codec registered so Python dicts can be sent as JSONB
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads,
        schema="pg_catalog", format="text",
    )

    existing = await conn.fetchval(
        "SELECT COUNT(*) FROM behavioral_screenings WHERE patient_id = $1",
        PATIENT_ID,
    )
    if existing > 0:
        print(f"PHQ-9 fixture already exists ({existing} rows). Skipping insert.")
        await conn.close()
        return

    sid = uuid.uuid4()
    # asyncpg infers JSONB from the column type — pass dicts directly (not strings)
    await conn.execute(
        """
        INSERT INTO behavioral_screenings
            (id, patient_id, instrument_key, domain, loinc_code,
             score, band, item_answers, triggered_critical,
             source_type, administered_at, data_source)
        VALUES
            ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """,
        sid,
        PATIENT_ID,
        "phq9",
        "depression_and_si",
        "44249-1",
        10,             # Total PHQ-9 score (moderate depression range)
        "moderate",
        ITEM_ANSWERS,          # pass dict — asyncpg encodes as JSONB
        TRIGGERED_CRITICAL,    # pass dict — asyncpg encodes as JSONB
        "ehr",
        ADMINISTERED_AT,
        "synthea",
    )

    print(f"✓  PHQ-9 fixture inserted  id={sid}")
    print(f"   patient:        Maria Chen ({PATIENT_ID})")
    print(f"   instrument:     phq9  score=10 (moderate)")
    print(f"   item 9:         {ITEM_ANSWERS['9']} (passive SI — 'several days')")
    print(f"   administered:   {ADMINISTERED_AT.date()}")
    print(f"   triggered:      {TRIGGERED_CRITICAL}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
