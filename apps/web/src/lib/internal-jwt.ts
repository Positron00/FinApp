import "server-only";

import { importPKCS8, SignJWT } from "jose";

import { config } from "./config";
import { uuidv7 } from "./ids";
import type { Role } from "./roles";

/**
 * Internal JWT minting (web LLD §3.4.1, PAY-600, R9, C10). EdDSA, 60 s, a fresh token per
 * upstream call. The key is imported on first use, so `next build` needs no secrets.
 */
export type Aud = "identity" | "payroll";

export interface TokenClaims {
  sub: string; // a user ID, or "service:web" on public flows
  roles: Role[];
  cid: string;
  sid?: string; // user tokens only
  name?: string; // user tokens only
}

let keyPromise: ReturnType<typeof importPKCS8> | undefined;

function signingKey() {
  keyPromise ??= importPKCS8(config().INTERNAL_JWT_PRIVATE_KEY, "EdDSA").catch((err: unknown) => {
    keyPromise = undefined; // retry on the next call rather than caching a failure
    throw err;
  });
  return keyPromise;
}

export async function internalToken(aud: Aud, p: TokenClaims): Promise<string> {
  return new SignJWT({
    roles: p.roles,
    cid: p.cid,
    ...(p.sid !== undefined ? { sid: p.sid } : {}),
    ...(p.name !== undefined ? { name: p.name } : {}),
  })
    .setProtectedHeader({ alg: "EdDSA", kid: config().INTERNAL_JWT_KID, typ: "JWT" })
    .setIssuer("web")
    .setAudience(aud)
    .setSubject(p.sub)
    .setIssuedAt()
    .setExpirationTime("60s")
    .setJti(uuidv7()) // C61: UUIDv7
    .sign(await signingKey());
}

/** Claims for a public flow (login, accept, reset): `service:web`, no name or sid (R9). */
export function serviceClaims(): TokenClaims {
  return { sub: "service:web", roles: [], cid: config().COMPANY_ID };
}

/** Tests only: drop the cached key. */
export function resetSigningKeyForTests(): void {
  keyPromise = undefined;
}
