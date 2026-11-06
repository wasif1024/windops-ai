import { act, renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ApiError } from "../../api/client";
import { messageFor, useChange } from "./useChange";

const problem = (code: string, status = 503) => new ApiError({ title: "x", status, code });

describe("a change", () => {
  it("is resent with the same key after an outage, so it happens once (GRP-007)", async () => {
    const { result } = renderHook(() => useChange());
    const keys: string[] = [];
    await act(async () => {
      await result.current.run(async (key) => {
        keys.push(key);
        throw problem("unavailable");
      });
    });
    expect(result.current.state).toEqual({ kind: "failed", message: messageFor(problem("unavailable")) });
    await act(async () => {
      await result.current.run(async (key) => keys.push(key));
    });
    await act(async () => {
      await result.current.run(async (key) => keys.push(key));
    });
    expect(keys[0]).toBe(keys[1]);
    // A new change after a success gets a new key.
    expect(keys[2]).not.toBe(keys[1]);
  });

  it("gets a new key after a refusal that sending again cannot fix", async () => {
    const { result } = renderHook(() => useChange());
    const keys: string[] = [];
    await act(async () => {
      await result.current.run(async (key) => {
        keys.push(key);
        throw problem("conflict", 409);
      });
    });
    await act(async () => {
      await result.current.run(async (key) => keys.push(key));
    });
    expect(keys[0]).not.toBe(keys[1]);
  });

  it("asks for a second factor when the service wants one (RFC 9470)", async () => {
    const { result } = renderHook(() => useChange());
    await act(async () => {
      await result.current.run(async () => {
        throw problem("step_up_required", 401);
      });
    });
    expect(result.current.state).toEqual({ kind: "step-up" });
  });

  it("speaks in its own words, whatever the service said", () => {
    expect(messageFor(new ApiError({ title: "internal detail", status: 403, code: "forbidden" }))).toBe(
      "You do not have permission to do this.",
    );
    expect(messageFor(new TypeError("Failed to fetch"))).toContain("did not reach WindOps");
  });
});
