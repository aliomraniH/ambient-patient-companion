import { NextResponse } from "next/server";
import { query } from "@/lib/db";

export async function GET() {
  try {
    const rows = await query(
      `SELECT p.id, p.mrn, p.first_name, p.last_name, p.birth_date, p.gender,
              o.score AS obt_score, o.primary_driver, o.trend_direction, o.confidence,
              pr.risk_score, pr.risk_tier
       FROM patients p
       LEFT JOIN LATERAL (
         SELECT score, primary_driver, trend_direction, confidence
         FROM obt_scores
         WHERE patient_id = p.id
         ORDER BY score_date DESC
         LIMIT 1
       ) o ON true
       LEFT JOIN LATERAL (
         SELECT risk_score, risk_tier
         FROM provider_risk_scores
         WHERE patient_id = p.id
         ORDER BY score_date DESC
         LIMIT 1
       ) pr ON true
       ORDER BY p.last_name, p.first_name`
    );
    return NextResponse.json({ patients: rows });
  } catch (error) {
    console.error("Patients GET error:", error);
    return NextResponse.json({ error: "Internal server error" }, { status: 500 });
  }
}

export async function POST(request: Request) {
  try {
    const body = await request.json();
    const { mrn, first_name, last_name, birth_date, gender, race, ethnicity,
            address_line, city, state, zip_code, insurance_type } = body;

    if (!mrn) {
      return NextResponse.json({ error: "mrn is required" }, { status: 400 });
    }

    const existing = await query("SELECT id FROM patients WHERE mrn = $1", [mrn]);
    if (existing.length > 0) {
      return NextResponse.json({ error: `MRN '${mrn}' already exists` }, { status: 409 });
    }

    const rows = await query(
      `INSERT INTO patients
         (mrn, first_name, last_name, birth_date, gender, race, ethnicity,
          address_line, city, state, zip_code, insurance_type, is_synthetic, data_source)
       VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,false,'manual')
       RETURNING id, mrn, first_name, last_name, birth_date, gender, created_at`,
      [mrn, first_name || null, last_name || null,
       birth_date || null, gender || null, race || null, ethnicity || null,
       address_line || null, city || null, state || null, zip_code || null,
       insurance_type || null]
    );

    return NextResponse.json({ patient: rows[0] }, { status: 201 });
  } catch (error) {
    console.error("Patients POST error:", error);
    return NextResponse.json({ error: "Internal server error" }, { status: 500 });
  }
}
