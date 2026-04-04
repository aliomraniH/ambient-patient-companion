"use client";

interface CareGap {
  id: string;
  gap_type: string;
  description: string;
  status: string;
  identified_date?: string;
  resolved_date?: string;
}

interface CareGapTrackerProps {
  gaps: CareGap[];
}

export default function CareGapTracker({ gaps }: CareGapTrackerProps) {
  if (gaps.length === 0) {
    return (
      <div className="rounded-xl border p-4 text-center text-gray-400">
        No care gaps identified
      </div>
    );
  }

  const open = gaps.filter((g) => g.status === "open");
  const resolved = gaps.filter((g) => g.status !== "open");
  const completionRate =
    gaps.length > 0 ? Math.round((resolved.length / gaps.length) * 100) : 0;

  return (
    <div className="space-y-4">
      {/* Summary bar */}
      <div className="rounded-xl border p-4">
        <div className="flex justify-between text-sm mb-2">
          <span className="text-gray-600">Care Gap Completion</span>
          <span className="font-semibold">
            {resolved.length}/{gaps.length} ({completionRate}%)
          </span>
        </div>
        <div
          role="progressbar"
          aria-valuenow={completionRate}
          aria-valuemin={0}
          aria-valuemax={100}
          className="h-2 bg-gray-200 rounded-full overflow-hidden"
        >
          <div
            className="h-full bg-emerald-500 rounded-full transition-all"
            style={{ width: `${completionRate}%` }}
          />
        </div>
      </div>

      {/* Individual gaps */}
      {gaps.map((gap) => (
        <div
          key={gap.id}
          className={`rounded-lg border p-3 ${
            gap.status === "open"
              ? "border-amber-200 bg-amber-50"
              : "border-emerald-200 bg-emerald-50"
          }`}
        >
          <div className="flex justify-between items-start">
            <div>
              <p className="font-medium text-sm">{gap.gap_type}</p>
              <p className="text-xs text-gray-600 mt-0.5">{gap.description}</p>
            </div>
            <span
              className={`text-xs px-2 py-0.5 rounded-full font-medium ${
                gap.status === "open"
                  ? "bg-amber-200 text-amber-800"
                  : "bg-emerald-200 text-emerald-800"
              }`}
            >
              {gap.status}
            </span>
          </div>
          <div
            role="progressbar"
            aria-valuenow={gap.status === "open" ? 0 : 100}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-1 bg-gray-200 rounded-full mt-2 overflow-hidden"
          >
            <div
              className={`h-full rounded-full ${
                gap.status === "open" ? "bg-amber-400 w-0" : "bg-emerald-500 w-full"
              }`}
            />
          </div>
        </div>
      ))}
    </div>
  );
}
