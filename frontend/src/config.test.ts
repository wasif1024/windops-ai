import { describe, expect, it } from "vitest";
import { parseConfig } from "./config";

describe("the frontend's settings", () => {
  it("reads the realm and client", () => {
    expect(parseConfig({ realm: "windops", clientId: "windops-web" })).toEqual({
      realm: "windops",
      clientId: "windops-web",
    });
  });

  it("refuses anything but plain names, so nothing odd reaches a URL", () => {
    for (const bad of [{}, { realm: "a/b", clientId: "x" }, { realm: "windops", clientId: "" }, null]) {
      expect(() => parseConfig(bad)).toThrow();
    }
  });
});
