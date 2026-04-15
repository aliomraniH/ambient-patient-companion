import { NextRequest, NextResponse } from "next/server";
import { query } from "@/lib/db";
import { requireBearerToken } from "@/lib/auth-middleware";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const authError = await requireBearerToken(request);
  if (authError) return authError;

  try {
    const { id } = await params;
    const rows = await query(
      `SELECT id, patient_id, score_date, score, primary_driver,
              trend_direction, confidence, domain_scores
       FROM obt_scores
       WHERE patient_id = $1
       ORDER BY score_date DESC
       LIMIT 1`,
      [id]
    );

    if (rows.length === 0) {
      return NextResponse.json(
        { error: "No OBT score found" },
        { status: 404 }
      );
    }

    return NextResponse.json(rows[0]);
  } catch (error) {
    console.error("OBT API error:", error);
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    );
  }
}
