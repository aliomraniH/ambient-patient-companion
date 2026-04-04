"use client";

import { useState } from "react";
import OBTScoreCard from "@/components/OBTScoreCard";
import VitalsChart from "@/components/VitalsChart";
import CheckInFlow from "@/components/CheckInFlow";
import SDoHFlags from "@/components/SDoHFlags";
import CareGapTracker from "@/components/CareGapTracker";
import AgentMemoryLog from "@/components/AgentMemoryLog";

type Tab = "today" | "vitals" | "health";

interface PatientTabsProps {
  patientId: string;
  obtData: {
    score: number;
    primary_driver: string;
    trend_direction: string;
    confidence: number;
    score_date: string;
    domain_scores?: Record<string, number>;
  } | null;
  sdohFlags: {
    domain: string;
    severity: string;
    screening_date?: string;
    notes?: string;
  }[];
  careGaps: {
    id: string;
    gap_type: string;
    description: string;
    status: string;
    identified_date?: string;
    resolved_date?: string;
  }[];
  memoryEpisodes: {
    id: string;
    episode_type: string;
    summary: string;
    occurred_at: string;
  }[];
}

export default function PatientTabs({
  patientId,
  obtData,
  sdohFlags,
  careGaps,
  memoryEpisodes,
}: PatientTabsProps) {
  const [activeTab, setActiveTab] = useState<Tab>("today");

  return (
    <div>
      {/* Tab buttons */}
      <div className="flex gap-1 border-b mb-6">
        {(
          [
            { key: "today", label: "Today" },
            { key: "vitals", label: "Vitals" },
            { key: "health", label: "My Health" },
          ] as { key: Tab; label: string }[]
        ).map((tab) => (
          <button
            key={tab.key}
            onClick={() => setActiveTab(tab.key)}
            className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
              activeTab === tab.key
                ? "border-blue-600 text-blue-600"
                : "border-transparent text-gray-500 hover:text-gray-700"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {/* Tab content */}
      {activeTab === "today" && (
        <div className="grid gap-6 md:grid-cols-2">
          <OBTScoreCard patientId={patientId} initialData={obtData} />
          <CheckInFlow patientId={patientId} />
        </div>
      )}

      {activeTab === "vitals" && (
        <VitalsChart patientId={patientId} />
      )}

      {activeTab === "health" && (
        <div className="space-y-6">
          <div>
            <h2 className="text-lg font-semibold mb-3">
              Social Determinants of Health
            </h2>
            <SDoHFlags flags={sdohFlags} />
          </div>
          <div>
            <h2 className="text-lg font-semibold mb-3">Care Gaps</h2>
            <CareGapTracker gaps={careGaps} />
          </div>
          <div>
            <h2 className="text-lg font-semibold mb-3">Agent Memory Log</h2>
            <AgentMemoryLog episodes={memoryEpisodes} />
          </div>
        </div>
      )}
    </div>
  );
}
