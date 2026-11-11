import { execFileSync } from "node:child_process";

import { repoRoot } from "./env";

/**
 * A short-lived internal JWT from `scripts/mint_token.py` (web LLD §9.2; C35, C44). Test-only:
 * the CLI reads the keys from `.env`; nothing is signed in TypeScript and no key is held here.
 *
 * - role-change spec: `issuer=web`, `aud=identity`, an admin user token for identity's direct port;
 * - recover spec (blocked case): `issuer=payout-worker`, `sub=service:payout-worker`,
 *   `aud=payroll`, for payroll's internal result endpoint.
 */
export interface MintArgs {
  aud: "identity" | "payroll";
  sub: string;
  roles?: string[];
  issuer?: "web" | "payout-worker";
  name?: string;
}

export function mintToken({ aud, sub, roles = [], issuer = "web", name }: MintArgs): string {
  const args = ["run", "--quiet", "python", "scripts/mint_token.py", "--aud", aud, "--sub", sub];
  if (roles.length > 0) args.push("--roles", roles.join(","));
  args.push("--issuer", issuer);
  if (name) args.push("--name", name);
  const out = execFileSync("uv", args, { cwd: repoRoot(), encoding: "utf8" }).trim();
  if (!/^[\w-]+\.[\w-]+\.[\w-]+$/.test(out)) throw new Error("mint_token.py printed no JWT");
  return out;
}
