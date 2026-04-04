"use client";

interface Patient {
  id: string;
  first_name: string;
  last_name: string;
  obt_score: number | null;
  risk_score: number | null;
  risk_tier: string | null;
  primary_driver: string | null;
}

interface ChaseListProps {
  patients: Patient[];
}

const tierColors: Record<string, string> = {
  high: "bg-red-100 text-red-800",
  moderate: "bg-amber-100 text-amber-800",
  low: "bg-emerald-100 text-emerald-800",
};

export default function ChaseList({ patients }: ChaseListProps) {
  // Sort by risk_score descending
  const sorted = [...patients].sort(
    (a, b) => (b.risk_score ?? 0) - (a.risk_score ?? 0)
  );

  if (sorted.length === 0) {
    return (
      <div className="rounded-xl border p-4 text-center text-gray-400">
        No patients found
      </div>
    );
  }

  return (
    <div className="rounded-xl border overflow-hidden">
      <table className="w-full text-sm">
        <thead className="bg-gray-50 text-left">
          <tr>
            <th className="px-4 py-3 font-medium text-gray-600">Patient</th>
            <th className="px-4 py-3 font-medium text-gray-600">OBT</th>
            <th className="px-4 py-3 font-medium text-gray-600">Risk</th>
            <th className="px-4 py-3 font-medium text-gray-600">Driver</th>
          </tr>
        </thead>
        <tbody className="divide-y">
          {sorted.map((patient, idx) => (
            <tr key={patient.id} className="hover:bg-gray-50">
              <td className="px-4 py-3">
                <a
                  href={`/patient/${patient.id}`}
                  data-testid="patient-name"
                  className="font-medium text-blue-600 hover:underline"
                >
                  {patient.last_name}, {patient.first_name}
                </a>
              </td>
              <td className="px-4 py-3">
                {patient.obt_score != null
                  ? Math.round(patient.obt_score)
                  : "—"}
              </td>
              <td className="px-4 py-3">
                {patient.risk_tier ? (
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs font-medium ${
                      tierColors[patient.risk_tier] || tierColors.low
                    }`}
                  >
                    {patient.risk_tier}
                  </span>
                ) : (
                  "—"
                )}
              </td>
              <td className="px-4 py-3 text-gray-500 capitalize">
                {patient.primary_driver?.replace("_", " ") || "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
