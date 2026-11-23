import { describe, expect, it } from "vitest";

import { missingDemoDays } from "../src/commands/up.js";

describe("demo telemetry gap", () => {
  const now = new Date("2026-11-22T10:00:00Z");
  it("fills the whole window when there is no telemetry", () => {
    expect(missingDemoDays(null, now)).toBe(30);
  });
  it("is zero when yesterday is covered", () => {
    expect(missingDemoDays({ first: "2026-10-23T00:00:00Z", last: "2026-11-21T23:45:00Z" }, now)).toBe(0);
  });
  it("fills only the missing days", () => {
    expect(missingDemoDays({ first: "2026-10-01T00:00:00Z", last: "2026-11-18T23:45:00Z" }, now)).toBe(3);
  });
  it("caps at the window", () => {
    expect(missingDemoDays({ first: "2025-01-01T00:00:00Z", last: "2025-02-01T00:00:00Z" }, now)).toBe(30);
  });
});
