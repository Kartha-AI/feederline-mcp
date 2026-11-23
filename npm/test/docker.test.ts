// SPDX-License-Identifier: Apache-2.0
import { describe, expect, it } from "vitest";

import { composeMajor } from "../src/docker.js";

describe("composeMajor", () => {
  it("parses plugin, standalone and desktop version strings", () => {
    expect(composeMajor("2.34.0-desktop.1")).toBe(2);
    expect(composeMajor("v2.29.1\n")).toBe(2);
    expect(composeMajor("5.0.1")).toBe(5);
    expect(composeMajor("1.29.2")).toBe(1);
    expect(composeMajor("garbage")).toBeNull();
  });
});
