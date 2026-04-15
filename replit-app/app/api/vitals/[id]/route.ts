import { NextRequest, NextResponse } from "next/server";
import { query } from "@/lib/db";
import { requireBearerToken } from "@/lib/auth-middleware";

export async function GET(
  request: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const authError = requireBearerToken(request);
  if (authError) return authError;

  try {
    const { id } = await params;
    const searchParams = request.nextUrl.searchParams;
    const days = parseInt(searchParams.get("days") || "30", 10);
    const metric = searchParams.get("metric") || null;

    let sql = `
      SELECT id, patient_id, metric_type, value, unit,
             measured_at, is_abnormal, day_of_month
      FROM biometric_readings
      WHERE patient_id = $1
        AND measured_at >= NOW() - ($2 || ' days')::interval
    `;
    const sqlParams: unknown[] = [id, days];

    if (metric) {
      sql += ` AND metric_type = $3`;
      sqlParams.push(metric);
    }

    sql += ` ORDER BY measured_at DESC`;

    const rows = await query(sql, sqlParams);

    return NextResponse.json({ readings: rows, count: rows.length });
  } catch (error) {
    console.error("Vitals API error:", error);
    return NextResponse.json(
      { error: "Internal server error" },
      { status: 500 }
    );
  }
}
