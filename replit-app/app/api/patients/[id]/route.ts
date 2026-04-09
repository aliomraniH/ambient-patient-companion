import { NextResponse } from "next/server";
import { query } from "@/lib/db";

interface RouteParams {
  params: Promise<{ id: string }>;
}

export async function GET(_req: Request, { params }: RouteParams) {
  const { id } = await params;
  try {
    const rows = await query(
      `SELECT p.id, p.mrn, p.first_name, p.last_name, p.birth_date, p.gender,
              p.race, p.ethnicity, p.address_line, p.city, p.state, p.zip_code,
              p.insurance_type, p.is_synthetic, p.data_source, p.created_at
       FROM patients p WHERE p.id = $1`,
      [id]
    );
    if (rows.length === 0) {
      return NextResponse.json({ error: "Patient not found" }, { status: 404 });
    }
    return NextResponse.json({ patient: rows[0] });
  } catch (error) {
    console.error("Patient GET error:", error);
    return NextResponse.json({ error: "Internal server error" }, { status: 500 });
  }
}

export async function PUT(request: Request, { params }: RouteParams) {
  const { id } = await params;
  try {
    const body = await request.json();
    const { first_name, last_name, birth_date, gender, race, ethnicity,
            address_line, city, state, zip_code, insurance_type } = body;

    const rows = await query(
      `UPDATE patients
       SET first_name   = COALESCE($2, first_name),
           last_name    = COALESCE($3, last_name),
           birth_date   = COALESCE($4, birth_date),
           gender       = COALESCE($5, gender),
           race         = COALESCE($6, race),
           ethnicity    = COALESCE($7, ethnicity),
           address_line = COALESCE($8, address_line),
           city         = COALESCE($9, city),
           state        = COALESCE($10, state),
           zip_code     = COALESCE($11, zip_code),
           insurance_type = COALESCE($12, insurance_type)
       WHERE id = $1
       RETURNING id, mrn, first_name, last_name, birth_date, gender`,
      [id, first_name || null, last_name || null, birth_date || null,
       gender || null, race || null, ethnicity || null,
       address_line || null, city || null, state || null, zip_code || null,
       insurance_type || null]
    );

    if (rows.length === 0) {
      return NextResponse.json({ error: "Patient not found" }, { status: 404 });
    }
    return NextResponse.json({ patient: rows[0] });
  } catch (error) {
    console.error("Patient PUT error:", error);
    return NextResponse.json({ error: "Internal server error" }, { status: 500 });
  }
}

export async function DELETE(_req: Request, { params }: RouteParams) {
  const { id } = await params;
  try {
    const rows = await query(
      "DELETE FROM patients WHERE id = $1 RETURNING id, mrn, first_name, last_name",
      [id]
    );
    if (rows.length === 0) {
      return NextResponse.json({ error: "Patient not found" }, { status: 404 });
    }
    return NextResponse.json({ deleted: rows[0] });
  } catch (error) {
    console.error("Patient DELETE error:", error);
    return NextResponse.json({ error: "Internal server error" }, { status: 500 });
  }
}
