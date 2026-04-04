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
    console.error("Patients API error:", error);
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    );
  }
}
