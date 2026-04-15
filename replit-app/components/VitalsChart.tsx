"use client";

import { useEffect, useState } from "react";
import { fetchVitals } from "@/lib/actions";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
} from "recharts";

interface Reading {
  metric_type: string;
  value: number;
  unit: string;
  recorded_at: string;
}

interface VitalsChartProps {
  patientId: string;
  readings?: Reading[];
}

type MetricTab = "BP" | "Glucose" | "HRV";
type TimeRange = "7d" | "30d" | "90d";

const metricMap: Record<MetricTab, string[]> = {
  BP: ["bp_systolic", "bp_diastolic"],
  Glucose: ["glucose_fasting", "glucose_postprandial"],
  HRV: ["hrv_rmssd"],
};

const rangeMap: Record<TimeRange, number> = {
  "7d": 7,
  "30d": 30,
  "90d": 90,
};

const lineColors = ["#2563eb", "#7c3aed", "#059669", "#d97706"];

export default function VitalsChart({
  patientId,
  readings: initialReadings,
}: VitalsChartProps) {
  const [activeTab, setActiveTab] = useState<MetricTab>("BP");
  const [timeRange, setTimeRange] = useState<TimeRange>("30d");
  const [readings, setReadings] = useState<Reading[]>(initialReadings || []);
  const [loading, setLoading] = useState(!initialReadings);

  useEffect(() => {
    setLoading(true);
    const days = rangeMap[timeRange];
    fetchVitals(patientId, days)
      .then((result) => {
        setReadings(result.readings || []);
        setLoading(false);
      })
      .catch(() => {
        setReadings([]);
        setLoading(false);
      });
  }, [patientId, timeRange]);

  const metricTypes = metricMap[activeTab];
  const filtered = readings.filter((r) => metricTypes.includes(r.metric_type));

  const chartData = filtered.reduce<
    Record<string, Record<string, number | string>>
  >((acc, reading) => {
    const date = new Date(reading.recorded_at).toLocaleDateString();
    if (!acc[date]) acc[date] = { date };
    acc[date][reading.metric_type] = Number(reading.value);
    return acc;
  }, {});
  const chartArray = Object.values(chartData);

  return (
    <div className="rounded-xl border p-4">
      <div className="flex justify-between items-center mb-4">
        <h3 className="font-semibold">Vitals</h3>
        <div className="flex gap-1">
          {(["7d", "30d", "90d"] as TimeRange[]).map((range) => (
            <button
              key={range}
              onClick={() => setTimeRange(range)}
              className={`px-2 py-1 text-xs rounded ${
                timeRange === range
                  ? "bg-blue-600 text-white"
                  : "bg-gray-100 text-gray-600 hover:bg-gray-200"
              }`}
            >
              {range}
            </button>
          ))}
        </div>
      </div>

      <div className="flex gap-1 mb-4">
        {(["BP", "Glucose", "HRV"] as MetricTab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-3 py-1 text-sm rounded ${
              activeTab === tab
                ? "bg-gray-800 text-white"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {loading ? (
        <div className="h-48 flex items-center justify-center text-gray-400">
          Loading...
        </div>
      ) : chartArray.length === 0 ? (
        <div className="h-48 flex items-center justify-center text-gray-400">
          No data for this period
        </div>
      ) : (
        <ResponsiveContainer width="100%" height={220}>
          <LineChart data={chartArray}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="date" tick={{ fontSize: 10 }} />
            <YAxis tick={{ fontSize: 10 }} />
            <Tooltip />
            {metricTypes.map((mt, i) => (
              <Line
                key={mt}
                type="monotone"
                dataKey={mt}
                stroke={lineColors[i]}
                strokeWidth={2}
                dot={false}
                name={mt.replace("_", " ")}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
