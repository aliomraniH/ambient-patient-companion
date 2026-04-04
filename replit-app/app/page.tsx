import { query } from "@/lib/db";
import Link from "next/link";

export const dynamic = "force-dynamic";

function getColorClass(score: number | null): string {
  if (score === null) return "bg-gray-100 text-gray-600";
  if (score >= 70) return "bg-emerald-100 text-emerald-800";
  if (score >= 40) return "bg-amber-100 text-amber-800";
  return "bg-red-100 text-red-800";
}

export default async function HomePage() {
  let patients: any[] = [];
  let dbError = false;

  try {
    patients = await query(
      `SELECT p.id, p.mrn, p.first_name, p.last_name, p.birth_date,
              o.score AS obt_score, o.primary_driver, o.trend_direction
       FROM patients p
       LEFT JOIN LATERAL (
         SELECT score, primary_driver, trend_direction
         FROM obt_scores
         WHERE patient_id = p.id
         ORDER BY score_date DESC LIMIT 1
       ) o ON true
       ORDER BY p.last_name, p.first_name`
    );
  } catch {
    dbError = true;
  }

  return (
    <div className="max-w-4xl mx-auto p-4 md:p-6">
      <div className="flex justify-between items-center mb-6">
        <div>
          <h1 className="text-2xl font-bold">Ambient Patient Companion</h1>
          <p className="text-sm text-gray-500">Select a patient to view their dashboard</p>
        </div>
        <Link
          href="/provider"
          className="px-4 py-2 bg-gray-800 text-white rounded-lg text-sm hover:bg-gray-700"
        >
          Provider Panel
        </Link>
      </div>

      {dbError && (
        <div className="rounded-xl border-2 border-amber-200 bg-amber-50 p-4 mb-6">
          <p className="text-amber-800 font-medium">
            Database not connected. Set DATABASE_URL environment variable.
          </p>
        </div>
      )}

      {patients.length === 0 && !dbError && (
        <div className="rounded-xl border p-8 text-center text-gray-400">
          <p>No patients found. Run the seed script to populate data.</p>
          <code className="text-xs mt-2 block">
            python mcp-server/seed.py --patients 10 --months 6
          </code>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {patients.map((p: any) => (
          <Link
            key={p.id}
            href={`/patient/${p.id}`}
            className="rounded-xl border p-4 hover:shadow-md transition-shadow"
          >
            <div className="flex justify-between items-start">
              <div>
                <h2 className="font-semibold">
                  {p.last_name}, {p.first_name}
                </h2>
                <p className="text-xs text-gray-400">MRN: {p.mrn}</p>
              </div>
              <span
                className={`text-sm font-bold px-2.5 py-1 rounded-full ${getColorClass(
                  p.obt_score ? Number(p.obt_score) : null
                )}`}
              >
                {p.obt_score != null ? Math.round(Number(p.obt_score)) : "\u2014"}
              </span>
            </div>
            {p.primary_driver && (
              <p className="text-xs text-gray-500 mt-2 capitalize">
                Driver: {p.primary_driver.replace("_", " ")}
              </p>
            )}
          </Link>
        ))}
      </div>
    </div>
  );
}
