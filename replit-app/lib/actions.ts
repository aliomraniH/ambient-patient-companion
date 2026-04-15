"use server";

import { query } from "@/lib/db";

export async function createPatient(data: {
  mrn: string;
  first_name?: string | null;
  last_name?: string | null;
  birth_date?: string | null;
  gender?: string | null;
  race?: string | null;
  ethnicity?: string | null;
  address_line?: string | null;
  city?: string | null;
  state?: string | null;
  zip_code?: string | null;
  insurance_type?: string | null;
}) {
  const result = await query(
    `INSERT INTO patients (mrn, first_name, last_name, birth_date, gender, race, ethnicity, address_line, city, state, zip_code, insurance_type)
     VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
     RETURNING id, mrn, first_name, last_name, birth_date, gender`,
    [
      data.mrn,
      data.first_name || null,
      data.last_name || null,
      data.birth_date || null,
      data.gender || null,
      data.race || null,
      data.ethnicity || null,
      data.address_line || null,
      data.city || null,
      data.state || null,
      data.zip_code || null,
      data.insurance_type || null,
    ]
  );
  return { patient: result[0] };
}

export async function updatePatient(
  id: string,
  data: Record<string, string | null>
) {
  const fields: string[] = [];
  const values: (string | null)[] = [];
  let idx = 1;

  const allowed = [
    "first_name",
    "last_name",
    "birth_date",
    "gender",
    "race",
    "ethnicity",
    "address_line",
    "city",
    "state",
    "zip_code",
    "insurance_type",
  ];

  for (const key of allowed) {
    if (key in data) {
      fields.push(`${key} = $${idx}`);
      values.push(data[key]);
      idx++;
    }
  }

  if (fields.length === 0) {
    throw new Error("No fields to update");
  }

  values.push(id);
  const result = await query(
    `UPDATE patients SET ${fields.join(", ")} WHERE id = $${idx}
     RETURNING id, mrn, first_name, last_name, birth_date, gender`,
    values
  );

  if (result.length === 0) throw new Error("Patient not found");
  return { patient: result[0] };
}

export async function deletePatient(id: string) {
  const result = await query(
    `DELETE FROM patients WHERE id = $1 RETURNING id, mrn`,
    [id]
  );
  if (result.length === 0) throw new Error("Patient not found");
  return { deleted: result[0] };
}

export async function fetchVitals(patientId: string, days: number) {
  const rows = await query(
    `SELECT metric_type, value, unit, recorded_at
     FROM vitals_readings
     WHERE patient_id = $1
       AND recorded_at >= NOW() - INTERVAL '1 day' * $2
     ORDER BY recorded_at`,
    [patientId, days]
  );
  return { readings: rows };
}

export async function submitCheckin(data: {
  patient_id: string;
  mood: string | null;
  mood_numeric: number;
  energy: string | null;
  stress_level: number | null;
  sleep_hours: number | null;
  notes: string;
}) {
  const result = await query(
    `INSERT INTO daily_checkins (patient_id, mood, mood_numeric, energy, stress_level, sleep_hours, notes)
     VALUES ($1, $2, $3, $4, $5, $6, $7)
     RETURNING id`,
    [
      data.patient_id,
      data.mood,
      data.mood_numeric,
      data.energy,
      data.stress_level,
      data.sleep_hours,
      data.notes,
    ]
  );
  return { checkin: result[0] };
}

export async function fetchObtScore(patientId: string) {
  const rows = await query(
    `SELECT score, primary_driver, trend_direction, confidence, score_date, domain_scores
     FROM obt_scores WHERE patient_id = $1
     ORDER BY score_date DESC LIMIT 1`,
    [patientId]
  );
  return rows[0] || null;
}
