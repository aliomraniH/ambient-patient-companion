/**
 * F16-F24: CheckInFlow component tests
 */
import React from "react";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import CheckInFlow from "@/components/CheckInFlow";

beforeEach(() => {
  global.fetch = jest.fn();
});

// F16: Starts on mood step
test("F16: starts on mood step", () => {
  render(<CheckInFlow patientId="test-id" />);
  expect(
    screen.getByText("How are you feeling today?")
  ).toBeInTheDocument();
});

// F17: Cannot advance without selecting (Next disabled)
test("F17: Next button disabled until selection made", () => {
  render(<CheckInFlow patientId="test-id" />);
  const nextBtn = screen.getByText("Next");
  expect(nextBtn).toBeDisabled();
});

// F18: Can advance after selecting mood
test("F18: can advance after selecting mood", () => {
  render(<CheckInFlow patientId="test-id" />);
  fireEvent.click(screen.getByText("good"));
  const nextBtn = screen.getByText("Next");
  expect(nextBtn).not.toBeDisabled();
});

// F19: Energy step after mood
test("F19: energy step appears after advancing from mood", () => {
  render(<CheckInFlow patientId="test-id" />);
  fireEvent.click(screen.getByText("good"));
  fireEvent.click(screen.getByText("Next"));
  expect(
    screen.getByText("How is your energy level?")
  ).toBeInTheDocument();
});

// F20: Stress step
test("F20: stress step appears in flow", () => {
  render(<CheckInFlow patientId="test-id" />);
  // Step 1: mood
  fireEvent.click(screen.getByText("good"));
  fireEvent.click(screen.getByText("Next"));
  // Step 2: energy
  fireEvent.click(screen.getByText("moderate"));
  fireEvent.click(screen.getByText("Next"));
  // Step 3: stress
  expect(
    screen.getByText("Rate your stress level (1-10)")
  ).toBeInTheDocument();
});

// F21: Sleep step
test("F21: sleep step appears in flow", () => {
  render(<CheckInFlow patientId="test-id" />);
  fireEvent.click(screen.getByText("good"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("moderate"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("5"));
  fireEvent.click(screen.getByText("Next"));
  expect(screen.getByText("Hours of sleep last night")).toBeInTheDocument();
});

// F22: Medications step
test("F22: medications step appears in flow", () => {
  render(<CheckInFlow patientId="test-id" />);
  fireEvent.click(screen.getByText("good"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("moderate"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("5"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.change(screen.getByPlaceholderText("e.g., 7.5"), {
    target: { value: "7.5" },
  });
  fireEvent.click(screen.getByText("Next"));
  expect(
    screen.getByText("Did you take your medications today?")
  ).toBeInTheDocument();
});

// F23: submit-checkin button present on last step
test("F23: submit-checkin button present on last step", () => {
  render(<CheckInFlow patientId="test-id" />);
  fireEvent.click(screen.getByText("good"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("moderate"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("5"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.change(screen.getByPlaceholderText("e.g., 7.5"), {
    target: { value: "7.5" },
  });
  fireEvent.click(screen.getByText("Next"));
  expect(screen.getByTestId("submit-checkin")).toBeInTheDocument();
});

// F24: Shows Check-in complete on success
test("F24: shows Check-in complete on successful POST", async () => {
  (global.fetch as jest.Mock).mockResolvedValue({
    ok: true,
    json: async () => ({ checkin_id: "abc", checkin_date: "2026-04-04" }),
  });

  render(<CheckInFlow patientId="test-id" />);
  fireEvent.click(screen.getByText("good"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("moderate"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("5"));
  fireEvent.click(screen.getByText("Next"));
  fireEvent.change(screen.getByPlaceholderText("e.g., 7.5"), {
    target: { value: "7.5" },
  });
  fireEvent.click(screen.getByText("Next"));
  fireEvent.click(screen.getByText("Yes"));
  fireEvent.click(screen.getByTestId("submit-checkin"));

  await waitFor(() => {
    expect(screen.getByText("Check-in complete")).toBeInTheDocument();
  });
});
