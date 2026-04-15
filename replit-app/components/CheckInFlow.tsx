"use client";

import { useState } from "react";
import { useAuth } from "@/components/SessionProvider";

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
const ENERGY_OPTIONS = ["low", "moderate", "high"] as const;
const STRESS_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10] as const;

type Step = "mood" | "energy" | "stress" | "sleep" | "medications";
const STEPS: Step[] = ["mood", "energy", "stress", "sleep", "medications"];

export default function CheckInFlow({ patientId }: CheckInFlowProps) {
  const { authFetch } = useAuth();
  const [stepIndex, setStepIndex] = useState(0);
  const [mood, setMood] = useState<string | null>(null);
  const [energy, setEnergy] = useState<string | null>(null);
  const [stress, setStress] = useState<number | null>(null);
  const [sleepHours, setSleepHours] = useState<number | null>(null);
  const [medsTaken, setMedsTaken] = useState<boolean | null>(null);
  const [status, setStatus] = useState<"idle" | "submitting" | "success" | "error">("idle");

  const currentStep = STEPS[stepIndex];

  const canAdvance = (): boolean => {
    switch (currentStep) {
      case "mood":
        return mood !== null;
      case "energy":
        return energy !== null;
      case "stress":
        return stress !== null;
      case "sleep":
        return sleepHours !== null;
      case "medications":
        return medsTaken !== null;
      default:
        return false;
    }
  };

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
      const res = await authFetch("/api/checkin", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          patient_id: patientId,
          mood,
          mood_numeric: mood ? MOOD_NUMERIC[mood] : 3,
          energy,
          stress_level: stress,
          sleep_hours: sleepHours,
          notes: medsTaken ? "Medications taken" : "Medications not taken",
        }),
      });

      if (res.ok) {
        setStatus("success");
      } else {
        setStatus("error");
      }
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
          className="mt-4 px-4 py-2 bg-red-600 text-white rounded-lg text-sm"
        >
          Try again
        </button>
      </div>
    );
  }

  return (
    <div className="rounded-xl border p-6">
      {/* Progress bar */}
      <div className="flex gap-1 mb-6">
        {STEPS.map((_, i) => (
          <div
            key={i}
            className={`h-1.5 flex-1 rounded-full ${
              i <= stepIndex ? "bg-blue-600" : "bg-gray-200"
            }`}
          />
        ))}
      </div>

      <p className="text-sm text-gray-500 mb-2">
        Step {stepIndex + 1} of {STEPS.length}
      </p>

      {/* Step content */}
      {currentStep === "mood" && (
        <div>
          <h3 className="text-lg font-semibold mb-4">How are you feeling today?</h3>
          <div className="flex flex-wrap gap-2">
            {MOOD_OPTIONS.map((option) => (
              <button
                key={option}
                onClick={() => setMood(option)}
                className={`px-4 py-2 rounded-lg text-sm font-medium capitalize transition-colors ${
                  mood === option
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                }`}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
      )}

      {currentStep === "energy" && (
        <div>
          <h3 className="text-lg font-semibold mb-4">How is your energy level?</h3>
          <div className="flex flex-wrap gap-2">
            {ENERGY_OPTIONS.map((option) => (
              <button
                key={option}
                onClick={() => setEnergy(option)}
                className={`px-4 py-2 rounded-lg text-sm font-medium capitalize transition-colors ${
                  energy === option
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                }`}
              >
                {option}
              </button>
            ))}
          </div>
        </div>
      )}

      {currentStep === "stress" && (
        <div>
          <h3 className="text-lg font-semibold mb-4">Rate your stress level (1-10)</h3>
          <div className="flex flex-wrap gap-2">
            {STRESS_OPTIONS.map((level) => (
              <button
                key={level}
                onClick={() => setStress(level)}
                className={`w-10 h-10 rounded-lg text-sm font-medium transition-colors ${
                  stress === level
                    ? "bg-blue-600 text-white"
                    : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                }`}
              >
                {level}
              </button>
            ))}
          </div>
        </div>
      )}

      {currentStep === "sleep" && (
        <div>
          <h3 className="text-lg font-semibold mb-4">Hours of sleep last night</h3>
          <input
            type="number"
            min="0"
            max="24"
            step="0.5"
            value={sleepHours ?? ""}
            onChange={(e) =>
              setSleepHours(e.target.value ? parseFloat(e.target.value) : null)
            }
            className="w-full px-4 py-3 border rounded-lg text-lg"
            placeholder="e.g., 7.5"
          />
        </div>
      )}

      {currentStep === "medications" && (
        <div>
          <h3 className="text-lg font-semibold mb-4">
            Did you take your medications today?
          </h3>
          <div className="flex gap-4">
            <button
              onClick={() => setMedsTaken(true)}
              className={`flex-1 px-4 py-3 rounded-lg text-sm font-medium transition-colors ${
                medsTaken === true
                  ? "bg-emerald-600 text-white"
                  : "bg-gray-100 text-gray-700 hover:bg-gray-200"
              }`}
            >
              Yes
            </button>
            <button
              onClick={() => setMedsTaken(false)}
              className={`flex-1 px-4 py-3 rounded-lg text-sm font-medium transition-colors ${
                medsTaken === false
                  ? "bg-red-600 text-white"
                  : "bg-gray-100 text-gray-700 hover:bg-gray-200"
              }`}
            >
              No
            </button>
          </div>
        </div>
      )}

      {/* Navigation */}
      <div className="mt-6 flex justify-between">
        <button
          onClick={handleBack}
          disabled={stepIndex === 0}
          className="px-4 py-2 text-sm text-gray-600 disabled:opacity-30"
        >
          Back
        </button>

        {stepIndex < STEPS.length - 1 ? (
          <button
            onClick={handleNext}
            disabled={!canAdvance()}
            className="px-6 py-2 bg-blue-600 text-white rounded-lg text-sm font-medium disabled:opacity-30 disabled:cursor-not-allowed"
          >
            Next
          </button>
        ) : (
          <button
            data-testid="submit-checkin"
            onClick={handleSubmit}
            disabled={!canAdvance() || status === "submitting"}
            className="px-6 py-2 bg-emerald-600 text-white rounded-lg text-sm font-medium disabled:opacity-30 disabled:cursor-not-allowed"
          >
            {status === "submitting" ? "Submitting..." : "Submit"}
          </button>
        )}
      </div>
    </div>
  );
}
