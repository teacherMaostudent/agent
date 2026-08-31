// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";
import { FactDetails } from "./FactDetails";

afterEach(cleanup);

it("renders a summary first and mounts the payload only after expansion", async () => {
  const { container } = render(<FactDetails title="Planner" description="受控计划" data={{ plan_id: "plan-1" }} />);
  expect(screen.getByText("Planner")).toBeTruthy();
  expect(container.querySelector("pre")).toBeNull();
  const details = container.querySelector("details")!;
  details.open = true;
  fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(screen.getByLabelText("Planner报文").textContent).toContain("plan-1"));
  details.open = false;
  fireEvent(details, new Event("toggle"));
  await waitFor(() => expect(container.querySelector("pre")).toBeNull());
});

it("does not carry an expanded old run payload into a different run", async () => {
  const { container, rerender } = render(<FactDetails key="run-a" title="RAG" description="证据" data={{ id: "old" }} />);
  container.querySelector("details")!.open = true;
  fireEvent(container.querySelector("details")!, new Event("toggle"));
  await waitFor(() => expect(container.querySelector("pre")).not.toBeNull());
  rerender(<FactDetails key="run-b" title="RAG" description="证据" data={{ id: "new" }} />);
  expect(container.querySelector("details")!.open).toBe(false);
  expect(container.querySelector("pre")).toBeNull();
});
