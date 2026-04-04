import { query } from "@/lib/db";
import PatientTabs from "@/components/PatientTabs";

export const dynamic = "force-dynamic";

interface PatientPageProps {
  params: Promise<{ id: string }>;
}

export default async function PatientPage({ params }: PatientPageProps) {
  const { id } = await params;

  const [patientRows, obtRows, sdohRows, gapRows, memoryRows] =
    await Promise.all([
      query(
        "SELECT id, first_name, last_name, birth_date, gender FROM patients WHERE id = $1",
        [id]
      ),
      query(
        `SELECT score, primary_driver, trend_direction, confidence, score_date, domain_scores
         FROM obt_scores WHERE patient_id = $1 ORDER BY score_date DESC LIMIT 1`,
        [id]
      ),
      query(
        "SELECT domain, severity, screening_date, notes FROM patient_sdoh_flags WHERE patient_id = $1",
        [id]
      ),
      query(
        `SELECT id, gap_type, description, status, identified_date, resolved_date
         FROM care_gaps WHERE patient_id = $1`,
        [id]
      ),
      query(
        `SELECT id, episode_type, summary, occurred_at
         FROM agent_memory_episodes WHERE patient_id = $1
         ORDER BY occurred_at DESC LIMIT 20`,
        [id]
      ),
    ]);

  const patient = patientRows[0];
  if (!patient) {
    return (
      <div className="max-w-4xl mx-auto p-6">
        <h1 className="text-2xl font-bold text-red-600">Patient not found</h1>
        <a href="/" className="text-blue-600 hover:underline mt-4 inline-block">
          Back to patient list
        </a>
      </div>
    );
  }

  const obtData = obtRows[0]
    ? {
        score: Number(obtRows[0].score),
        primary_driver: obtRows[0].primary_driver,
        trend_direction: obtRows[0].trend_direction,
        confidence: Number(obtRows[0].confidence),
        score_date: obtRows[0].score_date,
        domain_scores: obtRows[0].domain_scores,
      }
    : null;

  return (
    <div className="max-w-4xl mx-auto p-4 md:p-6">
      <div className="mb-6">
        <a href="/" className="text-sm text-blue-600 hover:underline">
          &larr; All Patients
        </a>
        <h1 className="text-2xl font-bold mt-2">
          {patient.first_name} {patient.last_name}
        </h1>
        <p className="text-sm text-gray-500">
          DOB:{" "}
          {patient.birth_date
            ? new Date(patient.birth_date).toLocaleDateString()
            : "—"}{" "}
          | Gender: {patient.gender || "—"}
        </p>
      </div>

      <PatientTabs
        patientId={id}
        obtData={obtData}
        sdohFlags={sdohRows as any[]}
        careGaps={gapRows as any[]}
        memoryEpisodes={memoryRows as any[]}
      />
    </div>
  );
}
