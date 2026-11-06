import { describe, expect, it } from "vitest";
import { ApiError, getJson } from "./api";

function answering(status: number, body: unknown, type = "application/problem+json"): typeof fetch {
  return async () => new Response(JSON.stringify(body), { status, headers: { "Content-Type": type } });
}

describe("the API client", () => {
  it("sends the token as a bearer, never as a cookie", async () => {
    let seen: RequestInit | undefined;
    const fetcher: typeof fetch = async (_url, init) => {
      seen = init;
      return new Response("{}", { status: 200 });
    };
    await getJson("/me", "abc", fetcher);
    expect((seen?.headers as Record<string, string>).Authorization).toBe("Bearer abc");
    expect(seen?.credentials).toBe("omit");
  });

  it("recognises a step-up request by its code", async () => {
    const error = await getJson("/x", "t", answering(401, { title: "x", status: 401, code: "step_up_required" })).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).needsStepUp).toBe(true);
  });

  it("copes with a refusal that is not a problem document, such as the gateway's", async () => {
    const error = await getJson("/x", "t", answering(403, "mismatched audience", "text/plain")).catch((e: unknown) => e);
    expect((error as ApiError).problem.status).toBe(403);
    expect((error as ApiError).needsStepUp).toBe(false);
  });
});
