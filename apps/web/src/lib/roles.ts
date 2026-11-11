/** Shared (server and browser). Roles from ADR 0002. */
export const ROLES = ["admin", "payroll_admin", "approver"] as const;
export type Role = (typeof ROLES)[number];

/** True when `held` contains at least one of `required` (any one suffices). */
export function hasAnyRole(held: readonly Role[], required: readonly Role[]): boolean {
  return required.some((r) => held.includes(r));
}
