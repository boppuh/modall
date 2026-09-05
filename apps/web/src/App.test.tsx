import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { App } from "./App";

describe("App", () => {
  it("identifies the registry shell and health", () => {
    render(<App />);

    expect(screen.getByRole("heading", { level: 1 }).textContent).toBe(
      "Capability operations, with exact lineage.",
    );
    expect(screen.getByRole("status").textContent).toContain("Foundation healthy");
  });
});
