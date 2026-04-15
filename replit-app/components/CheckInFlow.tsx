"use client";

import { useState } from "react";
import { submitCheckin } from "@/lib/actions";

interface CheckInFlowProps {
  patientId: string;
}

const MOOD_OPTIONS = ["bad", "low", "okay", "good", "great"] as const;
const MOOD_NUMERIC: Record<string, number> = {
  bad: 1,
  low: 2,
  okay: 3,
  good: 4,
  great: 5,
};
const MOOD_EMOJI: Record<string, string> = {
  bad: "\ud83d\ude1e",
  low: "\ud83d\ude15",
  okay: "\ud83d\ude10",
  good: "\ud83d\ude0a",
  great: "\ud83d\ude01",
};
const ENERGY_OPTIONS = ["low", "moderate", "high"] as const;
const STRESS_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] as const;

type Step = "mood" | "energy" | "stress" | "sleep" | "medications";
const STEPS: Step[] = ["mood", "energy", "stress", "sleep", "medications"];

export default function CheckInFlow({ patientId }: CheckInFlowProps) {
  const [stepIndex, setStepIndex] = useState(0);
  const [mood, setMood] = useState<string | null>(null);
  const [energy, setEnergy] = useState<string | null>(null);
  const [stress, setStress] = useState<number | null>(null);
  const [sleepHours, setSleepHours] = useState<number | null>(null);
  const [medsTaken, setMedsTaken] = useState<boolean | null>(null);
  const [status, setStatus] = useState<"idle" | "submitting" | "success" | "error">("idle");

  const currentStep = STEPS[stepIndex];

  const handleNext = () => {
    if (stepIndex < STEPS.length - 1) {
      setStepIndex(stepIndex + 1);
    }
  };

  const handleBack = () => {
    if (stepIndex > 0) {
      setStepIndex(stepIndex - 1);
    }
  };

  const handleSubmit = async () => {
    setStatus("submitting");
    try {
      await submitCheckin({
        patient_id: patientId,
        mood,
        mood_numeric: mood ? MOOD_NUMERIC[mood] : 3,
        energy,
        stress_level: stress,
        sleep_hours: sleepHours,
        notes: medsTaken ? "Medications taken" : "Medications not taken",
      });
      setStatus("success");
    } catch {
      setStatus("error");
    }
  };

  if (status === "success") {
    return (
      <div className="rounded-xl border p-6 text-center">
        <p className="text-lg font-semibold text-emerald-600">
          Check-in complete
        </p>
        <p className="text-sm text-gray-500 mt-2">
          Your responses have been recorded.
        </p>
      </div>
    );
  }

  if (status === "error") {
    return (
      <div className="rounded-xl border p-6 text-center">
        <p className="text-lg font-semibold text-red-600">
          Something went wrong
        </p>
        <button
          onClick={() => setStatus("idle")}
          className="mt-3 px-4 py-2 bg-gray-100 rounded-lg text-sm hover:bg-gray-200"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-xl border p-6">
      <div className="mb-4">
        <div className="flex gap-1 mb-2">
          {STEPS.map((s, i) => (
            <div
              key={s}
              className={`h-1 flex-1 rounded-full ${
                i <= stepIndex ? "bg-blue-500" : "bg-gray-200"
              }`}
            />
          ))}
        </div>
        <p className="text-xs text-gray-400">
          Step {stepIndex + 1} of {STEPS.length}
        </p>
      </div>

      {currentStep === "mood" && (
        <div>
          <h3 className="font-semibold mb-3">How are you feeling today?</h3>
          <div className="flex gap-2 flex-wrap">
            {MOOD_OPTIONS.map((m) => (
              <button
                key={m}
                onClick={() => setMood(m)}
                className={`px-4 py-2 rounded-lg text-sm capitalize ${
                  mood === m
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 hover:bg-gray-200"
                }`}
              >
                {MOOD_EMOJI[m]} {m}
              </button>
            ))}
          </div>
        </div>
      )}

      {currentStep === "energy" && (
        <div>
          <h3 className="font-semibold mb-3">Energy level?</h3>
          <div className="flex gap-2">
            {ENERGY_OPTIONS.map((e) => (
              <button
                key={e}
                onClick={() => setEnergy(e)}
                className={`px-4 py-2 rounded-lg text-sm capitalize ${
                  energy === e
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 hover:bg-gray-200"
                }`}
              >
                {e}
              </button>
            ))}
          </div>
        </div>
      )}

      {currentStep === "stress" && (
        <div>
          <h3 className="font-semibold mb-3">Stress level (1-10)?</h3>
          <div className="flex gap-1 flex-wrap">
            {STRESS_OPTIONS.map((s) => (
              <button
                key={s}
                onClick={() => setStress(s)}
                className={`w-9 h-9 rounded-lg text-sm ${
                  stress === s
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 hover:bg-gray-200"
                }`}
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      )}

      {currentStep === "sleep" && (
        <div>
          <h3 className="font-semibold mb-3">Hours of sleep last night?</h3>
          <input
            type="number"
            min={0}
            max={24}
            step={0.5}
            value={sleepHours ?? ""}
            onChange={(e) => setSleepHours(e.target.value ? Number(e.target.value) : null)}
            className="w-32 rounded-lg border px-3 py-2 text-sm"
            placeholder="e.g. 7.5"
          />
        </div>
      )}

      {currentStep === "medications" && (
        <div>
          <h3 className="font-semibold mb-3">
            Did you take your medications today?
          </h3>
          <div className="flex gap-2">
            <button
              onClick={() => setMedsTaken(true)}
              className={`px-4 py-2 rounded-lg text-sm ${
                medsTaken === true
                  ? "bg-emerald-600 text-white"
                  : "bg-gray-100 hover:bg-gray-200"
              }`}
            >
              Yes
            </button>
            <button
              onClick={() => setMedsTaken(false)}
              className={`px-4 py-2 rounded-lg text-sm ${
                medsTaken === false
                  ? "bg-red-500 text-white"
                  : "bg-gray-100 hover:bg-gray-200"
              }`}
            >
              No
            </button>
          </div>
        </div>
      )}

      <div className="flex justify-between mt-6">
        <button
          onClick={handleBack}
          disabled={stepIndex === 0}
          className="px-4 py-2 bg-gray-100 rounded-lg text-sm hover:bg-gray-200 disabled:opacity-30"
        >
          Back
        </button>
        {stepIndex === STEPS.length - 1 ? (
          <button
            onClick={handleSubmit}
            disabled={status === "submitting"}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700 disabled:opacity-50"
          >
            {status === "submitting" ? "Submitting..." : "Submit"}
          </button>
        ) : (
          <button
            onClick={handleNext}
            className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700"
          >
            Next
          </button>
        )}
      </div>
    </div>
  );
}
