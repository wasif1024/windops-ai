import { describe, expect, it } from "vitest";
import { contrastWithWhite, logoProblem, placeholdersIn } from "./brandingRules";

describe("the branding rules", () => {
  it("measures contrast as WCAG does", () => {
    expect(contrastWithWhite("#000000")).toBeCloseTo(21, 0);
    expect(contrastWithWhite("#ffffff")).toBeCloseTo(1, 1);
    // The default WindOps blue passes, and a pale yellow does not.
    expect(contrastWithWhite("#0b5394")).toBeGreaterThanOrEqual(4.5);
    expect(contrastWithWhite("#ffe066")).toBeLessThan(4.5);
  });

  it("finds the placeholders a text uses", () => {
    expect(placeholdersIn("Hi {{user_name}}, see {{ login_url }}")).toEqual(["user_name", "login_url"]);
  });

  it("refuses an SVG or a large logo", () => {
    expect(logoProblem(new File(["<svg/>"], "x.svg", { type: "image/svg+xml" }))).toMatch(/SVG/);
    expect(logoProblem(new File([new Uint8Array(300 * 1024)], "x.png", { type: "image/png" }))).toMatch(/256/);
    expect(logoProblem(new File([new Uint8Array(10)], "x.png", { type: "image/png" }))).toBeNull();
  });
});
