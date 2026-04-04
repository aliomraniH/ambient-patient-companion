"use client";

interface SDoHFlag {
  domain: string;
  severity: string;
  screening_date?: string;
  notes?: string;
}

interface SDoHFlagsProps {
  flags: SDoHFlag[];
}

const severityColors: Record<string, string> = {
  high: "bg-red-100 text-red-800 border-red-200",
  moderate: "bg-amber-100 text-amber-800 border-amber-200",
  low: "bg-blue-100 text-blue-800 border-blue-200",
};

const domainLabels: Record<string, string> = {
  food_access: "Food Access",
  housing_instability: "Housing Instability",
  transportation: "Transportation",
  social_isolation: "Social Isolation",
  financial_strain: "Financial Strain",
};

export default function SDoHFlags({ flags }: SDoHFlagsProps) {
  if (flags.length === 0) {
    return (
      <div className="rounded-xl border p-4 text-center text-gray-400">
        No SDoH flags identified
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {flags.map((flag) => (
        <div
          key={flag.domain}
          data-testid={`sdoh-${flag.domain}`}
          className={`rounded-lg border p-4 ${
            severityColors[flag.severity] || severityColors.low
          }`}
        >
          <div className="flex justify-between items-start">
            <div>
              <h4 className="font-semibold text-sm">
                {domainLabels[flag.domain] || flag.domain}
              </h4>
              {flag.notes && (
                <p className="text-xs mt-1 opacity-80">{flag.notes}</p>
              )}
            </div>
            <span className="text-xs font-medium uppercase px-2 py-0.5 rounded-full bg-white/50">
              {flag.severity}
            </span>
          </div>
        </div>
      ))}
    </div>
  );
}
