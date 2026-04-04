"use client";

import { useEffect, useState } from "react";

interface OBTScore {
  score: number;
  primary_driver: string;
  trend_direction: string;
  confidence: number;
  score_date: string;
  domain_scores?: Record<string, number>;
}

interface OBTScoreCardProps {
  patientId: string;
  initialData?: OBTScore | null;
}

function getColor(score: number): "green" | "amber" | "red" {
  if (score >= 70) return "green";
  if (score >= 40) return "amber";
  return "red";
}

function getTrendDirection(
  trend: string
): "up" | "down" | "right" {
  if (trend === "improving") return "up";
  if (trend === "declining") return "down";
  return "right";
}

const colorClasses = {
  green: "text-emerald-600 border-emerald-200 bg-emerald-50",
  amber: "text-amber-600 border-amber-200 bg-amber-50",
  red: "text-red-600 border-red-200 bg-red-50",
};

const strokeColors = {
  green: "#059669",
  amber: "#d97706",
  red: "#dc2626",
};

export default function OBTScoreCard({
  patientId,
  initialData = null,
}: OBTScoreCardProps) {
  const [data, setData] = useState<OBTScore | null>(initialData);

  useEffect(() => {
    if (!initialData) {
      fetch(`/api/obt/${patientId}`)
        .then((res) => (res.ok ? res.json() : null))
        .then((json) => {
          if (json && !json.error) setData(json);
        })
        .catch(() => {});
    }

    // SSE for real-time updates
    const evtSource = new EventSource(`/api/sse/${patientId}`);
    evtSource.addEventListener("obt-update", (event) => {
      try {
        const updated = JSON.parse(event.data);
        setData(updated);
      } catch {
        // ignore parse errors
      }
    });

    return () => evtSource.close();
  }, [patientId, initialData]);

  if (data === null) {
    return (
      <div
        data-testid="score-skeleton"
        className="animate-pulse rounded-xl border p-6 bg-gray-50"
      >
        <div className="h-24 w-24 rounded-full bg-gray-200 mx-auto" />
        <div className="mt-4 h-4 bg-gray-200 rounded w-3/4 mx-auto" />
        <div className="mt-2 h-3 bg-gray-200 rounded w-1/2 mx-auto" />
      </div>
    );
  }

  const color = getColor(data.score);
  const direction = getTrendDirection(data.trend_direction);

  // SVG ring parameters
  const radius = 45;
  const circumference = 2 * Math.PI * radius;
  const progress = (data.score / 100) * circumference;

  return (
    <div
      data-color={color}
      className={`rounded-xl border-2 p-6 ${colorClasses[color]}`}
    >
      {/* Score Ring */}
      <div className="flex justify-center">
        <svg width="120" height="120" className="transform -rotate-90">
          <circle
            cx="60"
            cy="60"
            r={radius}
            fill="none"
            stroke="#e5e7eb"
            strokeWidth="8"
          />
          <circle
            cx="60"
            cy="60"
            r={radius}
            fill="none"
            stroke={strokeColors[color]}
            strokeWidth="8"
            strokeDasharray={`${progress} ${circumference}`}
            strokeLinecap="round"
            style={{
              transition: "stroke-dasharray 0.5s ease",
            }}
          />
        </svg>
        <span className="absolute mt-10 text-3xl font-bold">
          {Math.round(data.score)}
        </span>
      </div>

      {/* Primary Driver */}
      <div className="mt-4 text-center">
        <p className="text-sm font-medium">
          Primary driver:{" "}
          <span className="font-semibold capitalize">
            {data.primary_driver?.replace("_", " ")}
          </span>
        </p>
      </div>

      {/* Trend Arrow */}
      <div className="mt-2 flex justify-center items-center gap-1">
        <span
          data-testid="trend-arrow"
          data-direction={direction}
          className="text-lg"
        >
          {direction === "up" && "\u2191"}
          {direction === "down" && "\u2193"}
          {direction === "right" && "\u2192"}
        </span>
        <span className="text-sm capitalize">{data.trend_direction}</span>
      </div>

      {/* Confidence Warning */}
      {data.confidence < 0.6 && (
        <p className="mt-3 text-center text-xs text-gray-500 italic">
          Limited data — score may vary
        </p>
      )}
    </div>
  );
}
