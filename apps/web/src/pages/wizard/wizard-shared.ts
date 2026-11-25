import { useQuery } from "@tanstack/react-query";
import { profileApi } from "@/api/profile-api";
import type { EvidenceDraft, UserProfile } from "@/api/profile-types";

/**
 * Shared wizard state shape: the draft profile the user is building plus the
 * raw CV text (persisted via the parse route, not PUT /profile).
 */
export interface WizardDraft {
  profile: UserProfile;
  cvText: string;
  description: string;
  /**
   * Phase 4.1a: free-text experience the user writes that is not in the CV.
   * Stored verbatim and never sent to an LLM for parsing.
   */
  additionalExperience: string;
  /** MF1: CV facts the owner confirms; Save writes them with the profile. */
  evidence: EvidenceDraft;
}

export function emptyEvidence(): EvidenceDraft {
  return { skills: [], experience: [], languages: [], education: [], projects: [], linked_year: null };
}

export function emptyDraft(): WizardDraft {
  return {
    profile: {
      name: "",
      education: "",
      experience: "",
      skills: [],
      job_preferences: [],
      target_countries: [],
      target_cities: [],
      work_arrangements: [],
      notes: [],
      role_families: [],
      custom_roles: [],
      location: "",
      citizenship: "",
      linkedin: "",
      gitlab: "",
      email: "",
      phone: "",
      visa_required: false,
    },
    cvText: "",
    description: "",
    additionalExperience: "",
    evidence: emptyEvidence(),
  };
}

/**
 * City chip-input logic (3.8g fix). Space and comma are chip delimiters;
 * per-city validation allows unicode letters (Finnish place names), spaces,
 * hyphens and apostrophes; pasted text is stripped to valid characters before
 * splitting so arbitrary symbols never become chips.
 */
export const CITY_INVALID_CHARS = /[^\p{L}\s'’,-]/gu;

export function normalizeCityToken(raw: string): string {
  return raw.replace(CITY_INVALID_CHARS, "").trim();
}

export function isValidCity(token: string): boolean {
  return token.length > 0 && /^[\p{L}][\p{L}\s'’-]*$/u.test(token);
}

/** Split raw input on space/comma delimiters; the trailing fragment stays pending. */
export function splitCityTokens(raw: string): { complete: string[]; pending: string } {
  const parts = raw.split(/[,\s]+/);
  const pending = parts.pop() ?? "";
  return { complete: parts, pending };
}

export interface CityIngest {
  /** Valid tokens to add (deduplicated against existing cities). */
  add: string[];
  /** Tokens that normalized to something but failed validation. */
  invalid: string[];
  /** The still-unfinished fragment to keep in the input. */
  pending: string;
}

export function ingestCityInput(existing: string[], raw: string): CityIngest {
  const normalized = raw.replace(CITY_INVALID_CHARS, "");
  const { complete, pending } = splitCityTokens(normalized);
  const add: string[] = [];
  const invalid: string[] = [];
  for (const token of complete.map(normalizeCityToken)) {
    if (!token) continue;
    if (isValidCity(token)) {
      if (!existing.includes(token) && !add.includes(token)) add.push(token);
    } else {
      invalid.push(token);
    }
  }
  return { add, invalid, pending };
}

export function useSegments() {
  return useQuery({
    queryKey: ["segments"],
    queryFn: () => profileApi.getSegments(),
    staleTime: Infinity,
  });
}

/**
 * Derive the selected segment list from the chosen role-family chips:
 * ordered intersection semantics (master_plan 2026-09-11) — a segment is kept
 * only if every chosen family maps to it, per-chip cap 3, mapping order
 * decides. On the mock side families already carry their ordered segment
 * lists; the real /segments endpoint will return the tech.yaml mapping.
 */
export function deriveSegments(chosenFamilies: string[], segments: ReturnType<typeof useSegments>["data"]) {
  if (!segments || chosenFamilies.length === 0) return [];
  const familyById = new Map(segments.role_families.map((f) => [f.id, f]));
  const perFamily: string[][] = chosenFamilies
    .map((id) => familyById.get(id)?.segments.slice(0, 3))
    .filter((s): s is string[] => Array.isArray(s));
  if (perFamily.length === 0) return [];
  return perFamily.reduce((acc, cur) => acc.filter((s) => cur.includes(s)));
}
/**
 * True once the user has saved real information. The API returns a row even
 * when it only holds defaults (or just a stored CV), so "a row exists" is not
 * enough: My information and Matches both key off this one definition.
 */
export function hasProfileInformation(profile: UserProfile | null | undefined): boolean {
  if (!profile) return false;
  return (
    profile.name.trim() !== "" ||
    profile.target_countries.length > 0 ||
    profile.role_families.length > 0
  );
}
