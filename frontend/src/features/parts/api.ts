// Parts lookup's calls to Web BFF (M9.4). The Inventory Service decides which stores each user may
// read; looking a part up holds nothing (FR-INVT-001, FR-INVT-002).
import { getJson } from "../../api/client";

export interface Model {
  manufacturer: string;
  model: string;
}

export interface Part {
  part_number: string;
  name: string;
  description: string;
  component_kind: string;
  unit: string;
  fits: Model[];
  fits_any_model: boolean;
  retired: boolean;
  version: number;
}

export interface Stock {
  wind_farm_id: string;
  on_hand: number;
  reserved: number;
  available: number;
  updated_at: string | null;
}

export interface PartSearch {
  turbine_id: string;
  wind_farm_id: string;
  model: Model | null;
  parts: { part: Part; stock: Stock | null }[];
  next_page_token: string;
}

export interface PartDetail {
  part: Part;
  stock: Stock[];
}

export interface Search {
  turbine_id: string;
  component_kind: string;
  query: string;
}

export function partsApi(token: string) {
  return {
    search: (search: Search) => {
      const query = new URLSearchParams({ page_size: "100" });
      if (search.turbine_id) query.set("turbine_id", search.turbine_id);
      if (search.component_kind) query.set("component_kind", search.component_kind);
      if (search.query) query.set("query", search.query);
      return getJson<PartSearch>(`/parts?${query.toString()}`, token);
    },
    detail: (partNumber: string) =>
      getJson<PartDetail>(`/parts/detail?${new URLSearchParams({ part_number: partNumber }).toString()}`, token),
  };
}

export type PartsApi = ReturnType<typeof partsApi>;

/** The models a part fits, in words. */
export function fitsText(part: Part): string {
  if (part.fits_any_model) return "Any model";
  return part.fits.map((m) => `${m.manufacturer} ${m.model}`).join(", ");
}
