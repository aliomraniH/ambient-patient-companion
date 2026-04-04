/**
 * F32-F38: Provider components tests (ChaseList, SDoH, CareGap, AgentMemory)
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import ChaseList from "@/components/ChaseList";
import SDoHFlags from "@/components/SDoHFlags";
import CareGapTracker from "@/components/CareGapTracker";
import AgentMemoryLog from "@/components/AgentMemoryLog";

// F32: patient-name on each row in ChaseList
test("F32: data-testid patient-name on each row", () => {
  const patients = [
    {
      id: "1",
      first_name: "John",
      last_name: "Doe",
      obt_score: 65,
      risk_score: 55,
      risk_tier: "moderate",
      primary_driver: "glucose",
    },
    {
      id: "2",
      first_name: "Jane",
      last_name: "Smith",
      obt_score: 80,
      risk_score: 30,
      risk_tier: "low",
      primary_driver: null,
    },
  ];
  render(<ChaseList patients={patients} />);
  const names = screen.getAllByTestId("patient-name");
  expect(names).toHaveLength(2);
});

// F33: ChaseList sorted by risk_score DESC
test("F33: ChaseList sorted by risk_score descending", () => {
  const patients = [
    {
      id: "1",
      first_name: "Low",
      last_name: "Risk",
      obt_score: 80,
      risk_score: 20,
      risk_tier: "low",
      primary_driver: null,
    },
    {
      id: "2",
      first_name: "High",
      last_name: "Risk",
      obt_score: 30,
      risk_score: 80,
      risk_tier: "high",
      primary_driver: null,
    },
  ];
  render(<ChaseList patients={patients} />);
  const names = screen.getAllByTestId("patient-name");
  expect(names[0]).toHaveTextContent("Risk, High");
  expect(names[1]).toHaveTextContent("Risk, Low");
});

// F34: sdoh-{domain} on each flag card
test("F34: data-testid sdoh-{domain} on each flag card", () => {
  const flags = [
    { domain: "food_access", severity: "high" },
    { domain: "transportation", severity: "moderate" },
  ];
  render(<SDoHFlags flags={flags} />);
  expect(screen.getByTestId("sdoh-food_access")).toBeInTheDocument();
  expect(screen.getByTestId("sdoh-transportation")).toBeInTheDocument();
});

// F35: role=progressbar on each care gap bar
test("F35: role=progressbar on each care gap bar", () => {
  const gaps = [
    {
      id: "1",
      gap_type: "A1C",
      description: "A1C test overdue",
      status: "open",
    },
    {
      id: "2",
      gap_type: "Eye exam",
      description: "Annual eye exam",
      status: "resolved",
    },
  ];
  render(<CareGapTracker gaps={gaps} />);
  const progressBars = screen.getAllByRole("progressbar");
  expect(progressBars.length).toBeGreaterThanOrEqual(1);
});

// F36: memory-episode on each AgentMemoryLog row
test("F36: data-testid memory-episode on each row", () => {
  const episodes = [
    {
      id: "1",
      episode_type: "crisis_detected",
      summary: "High BP detected",
      occurred_at: "2026-04-01T10:00:00Z",
    },
    {
      id: "2",
      episode_type: "insight",
      summary: "Improvement in adherence",
      occurred_at: "2026-04-02T10:00:00Z",
    },
  ];
  render(<AgentMemoryLog episodes={episodes} />);
  const rows = screen.getAllByTestId("memory-episode");
  expect(rows).toHaveLength(2);
});

// F37: AgentMemoryLog shows most recent first
test("F37: AgentMemoryLog shows most recent first", () => {
  const episodes = [
    {
      id: "1",
      episode_type: "crisis_detected",
      summary: "Older event",
      occurred_at: "2026-04-01T10:00:00Z",
    },
    {
      id: "2",
      episode_type: "insight",
      summary: "Newer event",
      occurred_at: "2026-04-03T10:00:00Z",
    },
  ];
  render(<AgentMemoryLog episodes={episodes} />);
  const rows = screen.getAllByTestId("memory-episode");
  expect(rows[0]).toHaveTextContent("Newer event");
});
