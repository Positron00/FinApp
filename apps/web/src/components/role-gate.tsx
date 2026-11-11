import type { ReactNode } from "react";

import { hasAnyRole, type Role } from "@/lib/roles";

/**
 * Shows `children` only to users holding one of `roles` (web LLD §1.4, §6.1). A UI convenience:
 * the BFF enforces every role itself (PAY-601).
 */
export function RoleGate({
  held,
  roles,
  children,
}: {
  held: readonly Role[];
  roles: readonly Role[];
  children: ReactNode;
}) {
  return hasAnyRole(held, roles) ? <>{children}</> : null;
}
