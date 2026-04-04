import { query } from "@/lib/db";
import ChaseList from "@/components/ChaseList";
import CareGapTracker from "@/components/CareGapTracker";

export const dynamic = "force-dynamic";

export default async function ProviderPage() {
  const [patientRows, gapRows] = await Promise.all([
    query(
      `SELECT p.id, p.first_name, p.last_name,
              o.score AS obt_score, o.primary_driver,
              pr.risk_score, pr.risk_tier
       FROM patients p
       LEFT JOIN LATERAL (
         SELECT score, primary_driver
         FROM obt_scores
         WHERE patient_id = p.id
         ORDER BY score_date DESC LIMIT 1
       ) o ON true
       LEFT JOIN LATERAL (
         SELECT risk_score, risk_tier
         FROM provider_risk_scores
         WHERE patient_id = p.id
         ORDER BY score_date DESC LIMIT 1
       ) pr ON true
       ORDER BY pr.risk_score DESC NULLS LAST`
    ),
    query(
      `SELECT cg.id, cg.gap_type, cg.description, cg.status,
              cg.identified_date, cg.resolved_date
       FROM care_gaps cg
       ORDER BY cg.status ASC, cg.identified_date DESC
       LIMIT 50`
    ),
  ]);

  const patients = patientRows.map((p) => ({
    id: p.id,
    first_name: p.first_name,
    last_name: p.last_name,
    obt_score: p.obt_score ? Number(p.obt_score) : null,
    risk_score: p.risk_score ? Number(p.risk_score) : null,
    risk_tier: p.risk_tier,
    primary_driver: p.primary_driver,
  }));

  const gaps = gapRows.map((g) => ({
    id: g.id,
    gap_type: g.gap_type,
    description: g.description,
    status: g.status,
    identified_date: g.identified_date,
    resolved_date: g.resolved_date,
  }));

  // Summary stats
  const highRisk = patients.filter((p) => p.risk_tier === "high").length;
  const avgObt =
    patients.filter((p) => p.obt_score !== null).length > 0
      ? Math.round(
          patients
            .filter((p) => p.obt_score !== null)
            .reduce((sum, p) => sum + (p.obt_score ?? 0), 0) /
            patients.filter((p) => p.obt_score !== null).length
        )
      : 0;

  return (
    <div className="max-w-6xl mx-auto p-4 md:p-6">
      <div className="mb-6">
        <a href="/" className="text-sm text-blue-600 hover:underline">
          &larr; Home
        </a>
        <h1 className="text-2xl font-bold mt-2">Provider Panel</h1>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">
        <div className="rounded-xl border p-4 text-center">
          <p className="text-3xl font-bold">{patients.length}</p>
          <p className="text-sm text-gray-500">Total Patients</p>
        </div>
        <div className="rounded-xl border p-4 text-center">
          <p className="text-3xl font-bold text-red-600">{highRisk}</p>
          <p className="text-sm text-gray-500">High Risk</p>
        </div>
        <div className="rounded-xl border p-4 text-center">
          <p className="text-3xl font-bold">{avgObt}</p>
          <p className="text-sm text-gray-500">Avg OBT Score</p>
        </div>
        <div className="rounded-xl border p-4 text-center">
          <p className="text-3xl font-bold text-amber-600">
            {gaps.filter((g) => g.status === "open").length}
          </p>
          <p className="text-sm text-gray-500">Open Care Gaps</p>
        </div>
      </div>

      {/* Chase list */}
      <div className="mb-8">
        <h2 className="text-lg font-semibold mb-3">
          Patient Chase List (by Risk Score)
        </h2>
        <ChaseList patients={patients} />
      </div>

      {/* Care gaps */}
      {gaps.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-3">Care Gap Tracker</h2>
          <CareGapTracker gaps={gaps} />
        </div>
      )}
    </div>
  );
}
