import { NextRequest, NextResponse } from "next/server";
import { query } from "@/lib/db";
import { requireBearerToken } from "@/lib/auth-middleware";

export async function POST(request: NextRequest) {
  const authError = await requireBearerToken(request);
  if (authError) return authError;

  try {
    const body = await request.json();
    const {
      patient_id,
      mood,
      mood_numeric,
      energy,
      stress_level,
      sleep_hours,
      notes,
    } = body;

    if (!patient_id || !mood) {
      return NextResponse.json(
        { error: "patient_id and mood are required" },
        { status: 400 }
      );
    }

    const rows = await query(
      `INSERT INTO daily_checkins
         (id, patient_id, checkin_date, mood, mood_numeric,
          energy, stress_level, sleep_hours, notes, data_source)
       VALUES (gen_random_uuid(), $1, CURRENT_DATE, $2, $3, $4, $5, $6, $7, 'manual')
       ON CONFLICT (patient_id, checkin_date) DO UPDATE SET
         mood = EXCLUDED.mood,
         mood_numeric = EXCLUDED.mood_numeric,
         energy = EXCLUDED.energy,
         stress_level = EXCLUDED.stress_level,
         sleep_hours = EXCLUDED.sleep_hours,
         notes = EXCLUDED.notes,
         data_source = EXCLUDED.data_source
       RETURNING id, checkin_date`,
      [
        patient_id,
        mood,
        mood_numeric ?? 3,
        energy ?? "moderate",
        stress_level ?? 5,
        sleep_hours ?? 7.0,
        notes ?? null,
      ]
    );

    return NextResponse.json(
      { checkin_id: rows[0].id, checkin_date: rows[0].checkin_date },
      { status: 201 }
    );
  } catch (error) {
    console.error("Checkin API error:", error);
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    );
  }
}
