import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { AdminApi, User } from "../api";
import { UsersPage } from "./UsersPage";

const pat: User = {
  member_id: "m-1",
  email: "pat@north-sea.example",
  first_name: "Pat",
  last_name: "Lee",
  enabled: true,
  created_at: null,
};

function fakeApi(overrides: Partial<AdminApi> = {}): AdminApi {
  return {
    users: vi.fn(async () => ({ users: [pat], next_page_token: "" })),
    createUser: vi.fn(async () => ({
      user: { ...pat, member_id: "m-2", email: "new@north-sea.example" },
      temporary_password: "Wo1!temporary",
      already_created: false,
    })),
    ...overrides,
  } as unknown as AdminApi;
}

function show(api: AdminApi) {
  render(
    <MemoryRouter>
      <UsersPage api={api} />
    </MemoryRouter>,
  );
}

afterEach(cleanup);

describe("the users page", () => {
  it("lists the tenant's users, each linking to their own page", async () => {
    show(fakeApi());
    const link = await screen.findByRole("link", { name: /pat@north-sea.example/ });
    expect(link.getAttribute("href")).toBe("/?tab=users&user=m-1");
    expect(screen.getByText("Active")).toBeTruthy();
  });

  it("shows a new user's temporary password once, then forgets it", async () => {
    const api = fakeApi();
    show(api);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: " New@North-Sea.example " } });
    fireEvent.click(screen.getByRole("button", { name: "Add user" }));
    expect((await screen.findByTestId("temporary-password")).textContent).toBe("Wo1!temporary");
    const [key, body] = vi.mocked(api.createUser).mock.calls[0] ?? [];
    expect(key).toMatch(/^[0-9a-f-]{36}$/);
    expect(body).toEqual({ email: "new@north-sea.example", first_name: "", last_name: "" });
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(screen.queryByTestId("temporary-password")).toBeNull();
  });

  it("catches a mistyped email before sending anything", async () => {
    const api = fakeApi();
    show(api);
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "not-an-email" } });
    fireEvent.submit(screen.getByRole("button", { name: "Add user" }).closest("form") as HTMLFormElement);
    expect(await screen.findByText(/Enter an email address/)).toBeTruthy();
    expect(api.createUser).not.toHaveBeenCalled();
  });
});
