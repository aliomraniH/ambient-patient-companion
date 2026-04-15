"use client";

import { useEffect, useState } from "react";
import { useAuth } from "@/components/SessionProvider";
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
  measured_at: string;
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
  const { authFetch } = useAuth();
  const [activeTab, setActiveTab] = useState<MetricTab>("BP");
  const [timeRange, setTimeRange] = useState<TimeRange>("30d");
  const [readings, setReadings] = useState<Reading[]>(initialReadings || []);
  const [loading, setLoading] = useState(!initialReadings);

  useEffect(() => {
    setLoading(true);
    const days = rangeMap[timeRange];
    authFetch(`/api/vitals/${patientId}?days=${days}`)
      .then((res) => (res.ok ? res.json() : { readings: [] }))
      .then((json) => {
        setReadings(json.readings || []);
        setLoading(false);
      })
      .catch(() => {
        setReadings([]);
        setLoading(false);
      });
  }, [patientId, timeRange]);

  const metricTypes = metricMap[activeTab];
  const filtered = readings.filter((r) => metricTypes.includes(r.metric_type));

  // Group by timestamp for chart
  const chartData = filtered.reduce<
    Record<string, Record<string, number | string>>
  >((acc, r) => {
    const key = new Date(r.measured_at).toLocaleDateString();
    if (!acc[key]) acc[key] = { date: key };
    acc[key][r.metric_type] = r.value;
    return acc;
  }, {});

  const sortedData = Object.values(chartData).sort(
    (a, b) =>
      new Date(a.date as string).getTime() -
      new Date(b.date as string).getTime()
  );

  return (
    <div data-testid="vitals-chart-container" className="rounded-xl border p-4">
      {/* Metric Tabs */}
      <div className="flex gap-2 mb-4">
        {(["BP", "Glucose", "HRV"] as MetricTab[]).map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`px-4 py-1.5 rounded-lg text-sm font-medium transition-colors ${
              activeTab === tab
                ? "bg-blue-600 text-white"
                : "bg-gray-100 text-gray-700 hover:bg-gray-200"
            }`}
          >
            {tab}
          </button>
        ))}
      </div>

      {/* Time Range */}
      <div className="flex gap-2 mb-4">
        {(["7d", "30d", "90d"] as TimeRange[]).map((range) => (
          <button
            key={range}
            onClick={() => setTimeRange(range)}
            className={`px-3 py-1 rounded text-xs font-medium transition-colors ${
              timeRange === range
                ? "bg-gray-800 text-white"
                : "bg-gray-100 text-gray-600 hover:bg-gray-200"
            }`}
          >
            {range}
          </button>
        ))}
      </div>

      {/* Chart */}
      {filtered.length === 0 ? (
        <p className="text-center text-gray-400 py-8">No data yet</p>
      ) : (
        <div data-count={sortedData.length}>
          <ResponsiveContainer width="100%" height={280}>
            <LineChart data={sortedData}>
              <CartesianGrid strokeDasharray="3 3" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 11 }}
                interval="preserveStartEnd"
              />
              <YAxis tick={{ fontSize: 11 }} />
              <Tooltip />
              {metricTypes.map((metric, i) => (
                <Line
                  key={metric}
                  type="monotone"
                  dataKey={metric}
                  stroke={lineColors[i]}
                  strokeWidth={2}
                  dot={false}
                  name={metric.replace("_", " ")}
                />
              ))}
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
