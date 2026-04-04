/**
 * F1-F8: OBTScoreCard component tests
 */
import React from "react";
import { render, screen } from "@testing-library/react";
import OBTScoreCard from "@/components/OBTScoreCard";

// Mock fetch and EventSource
beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue({
    ok: false,
    json: async () => ({}),
  });
  global.EventSource = jest.fn().mockImplementation(() => ({
    addEventListener: jest.fn(),
    close: jest.fn(),
  })) as any;
});

// F1: Shows skeleton when data is null
test("F1: renders skeleton when data is null", () => {
  render(<OBTScoreCard patientId="test-id" />);
  expect(screen.getByTestId("score-skeleton")).toBeInTheDocument();
});

// F2: data-color green when score >= 70
test("F2: data-color is green when score >= 70", () => {
  const data = {
    score: 75,
    primary_driver: "blood_pressure",
    trend_direction: "stable",
    confidence: 1.0,
    score_date: "2026-04-04",
  };
  const { container } = render(
    <OBTScoreCard patientId="test-id" initialData={data} />
  );
  const card = container.querySelector("[data-color]");
  expect(card?.getAttribute("data-color")).toBe("green");
});

// F3: data-color amber when score 40-69
test("F3: data-color is amber when score is 40-69", () => {
  const data = {
    score: 55,
    primary_driver: "glucose",
    trend_direction: "stable",
    confidence: 1.0,
    score_date: "2026-04-04",
  };
  const { container } = render(
    <OBTScoreCard patientId="test-id" initialData={data} />
  );
  const card = container.querySelector("[data-color]");
  expect(card?.getAttribute("data-color")).toBe("amber");
});

// F4: data-color red when score < 40
test("F4: data-color is red when score < 40", () => {
  const data = {
    score: 25,
    primary_driver: "behavioral",
    trend_direction: "declining",
    confidence: 1.0,
    score_date: "2026-04-04",
  };
  const { container } = render(
    <OBTScoreCard patientId="test-id" initialData={data} />
  );
  const card = container.querySelector("[data-color]");
  expect(card?.getAttribute("data-color")).toBe("red");
});

// F5: trend-arrow is always present
test("F5: trend-arrow is always present", () => {
  const data = {
    score: 60,
    primary_driver: "sleep",
    trend_direction: "stable",
    confidence: 1.0,
    score_date: "2026-04-04",
  };
  render(<OBTScoreCard patientId="test-id" initialData={data} />);
  expect(screen.getByTestId("trend-arrow")).toBeInTheDocument();
});

// F6: trend direction up for improving
test("F6: data-direction is up when improving", () => {
  const data = {
    score: 70,
    primary_driver: "adherence",
    trend_direction: "improving",
    confidence: 1.0,
    score_date: "2026-04-04",
  };
  render(<OBTScoreCard patientId="test-id" initialData={data} />);
  const arrow = screen.getByTestId("trend-arrow");
  expect(arrow.getAttribute("data-direction")).toBe("up");
});

// F7: trend direction down for declining
test("F7: data-direction is down when declining", () => {
  const data = {
    score: 30,
    primary_driver: "glucose",
    trend_direction: "declining",
    confidence: 1.0,
    score_date: "2026-04-04",
  };
  render(<OBTScoreCard patientId="test-id" initialData={data} />);
  const arrow = screen.getByTestId("trend-arrow");
  expect(arrow.getAttribute("data-direction")).toBe("down");
});

// F8: Shows limited data warning when confidence < 0.6
test("F8: shows limited data warning when confidence < 0.6", () => {
  const data = {
    score: 50,
    primary_driver: "blood_pressure",
    trend_direction: "stable",
    confidence: 0.4,
    score_date: "2026-04-04",
  };
  render(<OBTScoreCard patientId="test-id" initialData={data} />);
  expect(
    screen.getByText("Limited data — score may vary")
  ).toBeInTheDocument();
});
