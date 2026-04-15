"use client";

import { useEffect, useState } from "react";
import { fetchObtScore } from "@/lib/actions";

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

const COLORS: Record<string, string> = {
  green: "#059669",
  amber: "#d97706",
  red: "#dc2626",
};

function getColor(score: number): string {
  if (score >= 70) return COLORS.green;
  if (score >= 40) return COLORS.amber;
  return COLORS.red;
}

function getTrendIcon(direction: string): string {
  switch (direction) {
    case "improving":
      return "\u2191";
    case "declining":
      return "\u2193";
    default:
      return "\u2192";
  }
}

export default function OBTScoreCard({
  patientId,
  initialData = null,
}: OBTScoreCardProps) {
  const [data, setData] = useState<OBTScore | null>(initialData);

  useEffect(() => {
    if (!initialData) {
      fetchObtScore(patientId)
        .then((result) => {
          if (result) setData(result as OBTScore);
        })
        .catch(() => {});
    }

    const interval = setInterval(async () => {
      try {
        const result = await fetchObtScore(patientId);
        if (result) setData(result as OBTScore);
      } catch {
      }
    }, 30000);

    return () => clearInterval(interval);
  }, [patientId, initialData]);

  if (data === null) {
    return (
      <div
        className="rounded-xl border p-4"
        style={{ borderLeft: "4px solid #9ca3af" }}
      >
        <h3 className="font-semibold text-sm text-gray-500">OBT Score</h3>
        <p className="text-gray-400 text-xs mt-1">No score available</p>
      </div>
    );
  }

  const color = getColor(data.score);
  const trend = getTrendIcon(data.trend_direction);

  return (
    <div
      className="rounded-xl border p-4"
      style={{ borderLeft: `4px solid ${color}` }}
    >
      <div className="flex justify-between items-start">
        <div>
          <h3 className="font-semibold text-sm text-gray-500">OBT Score</h3>
          <div className="flex items-baseline gap-2 mt-1">
            <span className="text-3xl font-bold" style={{ color }}>
              {Math.round(data.score)}
            </span>
            <span className="text-lg" title={data.trend_direction}>
              {trend}
            </span>
          </div>
        </div>
        <div className="text-right">
          <p className="text-xs text-gray-400">
            {new Date(data.score_date).toLocaleDateString()}
          </p>
          <p className="text-xs text-gray-500 mt-1 capitalize">
            Driver: {data.primary_driver?.replace("_", " ")}
          </p>
          <p className="text-xs text-gray-400 mt-0.5">
            Confidence: {(data.confidence * 100).toFixed(0)}%
          </p>
        </div>
      </div>

      {data.domain_scores && (
        <div className="mt-3 grid grid-cols-3 gap-2">
          {Object.entries(data.domain_scores).map(([domain, score]) => (
            <div key={domain} className="text-center">
              <p className="text-[10px] text-gray-400 capitalize">
                {domain.replace("_", " ")}
              </p>
              <p className="text-sm font-semibold">{Math.round(Number(score))}</p>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
