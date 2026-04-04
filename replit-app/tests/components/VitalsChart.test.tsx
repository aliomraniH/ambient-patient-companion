/**
 * F9-F15: VitalsChart component tests
 */
import React from "react";
import { render, screen, fireEvent } from "@testing-library/react";
import VitalsChart from "@/components/VitalsChart";

// Mock recharts to avoid rendering issues in JSDOM
jest.mock("recharts", () => ({
  LineChart: ({ children }: any) => <div data-testid="mock-line-chart">{children}</div>,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ResponsiveContainer: ({ children }: any) => <div>{children}</div>,
}));

beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    json: async () => ({
      readings: [
        {
          metric_type: "bp_systolic",
          value: 140,
          unit: "mmHg",
          measured_at: "2026-04-01T08:00:00Z",
        },
        {
          metric_type: "bp_diastolic",
          value: 85,
          unit: "mmHg",
          measured_at: "2026-04-01T08:00:00Z",
        },
      ],
      count: 2,
    }),
  });
});

// F9: vitals-chart-container is present
test("F9: renders vitals-chart-container", () => {
  render(<VitalsChart patientId="test-id" readings={[]} />);
  expect(screen.getByTestId("vitals-chart-container")).toBeInTheDocument();
});

// F10: BP tab button present
test("F10: BP tab button present", () => {
  render(<VitalsChart patientId="test-id" readings={[]} />);
  expect(screen.getByText("BP")).toBeInTheDocument();
});

// F11: Glucose tab button present
test("F11: Glucose tab button present", () => {
  render(<VitalsChart patientId="test-id" readings={[]} />);
  expect(screen.getByText("Glucose")).toBeInTheDocument();
});

// F12: HRV tab button present
test("F12: HRV tab button present", () => {
  render(<VitalsChart patientId="test-id" readings={[]} />);
  expect(screen.getByText("HRV")).toBeInTheDocument();
});

// F13: 7d time range button present
test("F13: time range buttons present", () => {
  render(<VitalsChart patientId="test-id" readings={[]} />);
  expect(screen.getByText("7d")).toBeInTheDocument();
  expect(screen.getByText("30d")).toBeInTheDocument();
  expect(screen.getByText("90d")).toBeInTheDocument();
});

// F14: No data yet when readings is empty
test("F14: shows No data yet when readings is empty", () => {
  // Override the default mock to return empty readings
  (global.fetch as jest.Mock).mockResolvedValue({
    ok: true,
    json: async () => ({ readings: [], count: 0 }),
  });
  render(<VitalsChart patientId="test-id" readings={[]} />);
  expect(screen.getByText("No data yet")).toBeInTheDocument();
});

// F15: data-count attribute updates on chart
test("F15: data-count attribute on chart element", () => {
  const readings = [
    {
      metric_type: "bp_systolic",
      value: 140,
      unit: "mmHg",
      measured_at: "2026-04-01T08:00:00Z",
    },
    {
      metric_type: "bp_diastolic",
      value: 85,
      unit: "mmHg",
      measured_at: "2026-04-01T08:00:00Z",
    },
  ];
  const { container } = render(
    <VitalsChart patientId="test-id" readings={readings} />
  );
  const chartEl = container.querySelector("[data-count]");
  expect(chartEl).toBeInTheDocument();
});
