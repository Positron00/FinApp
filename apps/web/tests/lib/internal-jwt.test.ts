import { decodeProtectedHeader, importSPKI, jwtVerify } from "jose";

import { internalToken, serviceClaims } from "@/lib/internal-jwt";

import { applyEnv, COMPANY_ID, TEST_ENV, TEST_PUBLIC_PEM } from "../helpers/env";
import { USER_ID } from "../helpers/request";

const userClaims = {
  sub: USER_ID,
  roles: ["approver" as const],
  cid: COMPANY_ID,
  sid: "a".repeat(32),
  name: "Olive Owner",
};

describe("internalToken", () => {
  // PAY-600: header alg=EdDSA with the kid; claims iss, aud, sub, roles, cid, sid, name, 60 s, jti.
  it("mints a verifiable EdDSA user token", async () => {
    const token = await internalToken("payroll", userClaims);
    expect(decodeProtectedHeader(token)).toEqual({ alg: "EdDSA", kid: "test-web-1", typ: "JWT" });
    const key = await importSPKI(TEST_PUBLIC_PEM, "EdDSA");
    const { payload } = await jwtVerify(token, key, { issuer: "web", audience: "payroll" });
    expect(payload).toMatchObject({
      sub: USER_ID,
      roles: ["approver"],
      cid: COMPANY_ID,
      sid: "a".repeat(32),
      name: "Olive Owner",
    });
    expect(payload.exp! - payload.iat!).toBe(60);
    expect(payload.jti).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    ); // C61
    expect(Object.keys(payload).sort()).toEqual([
      "aud",
      "cid",
      "exp",
      "iat",
      "iss",
      "jti",
      "name",
      "roles",
      "sid",
      "sub",
    ]);
  });

  // PAY-600: aud per target, and a unique jti per token.
  it("sets aud per target and a fresh jti", async () => {
    const a = await internalToken("identity", userClaims);
    const b = await internalToken("identity", userClaims);
    const key = await importSPKI(TEST_PUBLIC_PEM, "EdDSA");
    const pa = (await jwtVerify(a, key, { audience: "identity" })).payload;
    const pb = (await jwtVerify(b, key, { audience: "identity" })).payload;
    expect(pa.jti).not.toBe(pb.jti);
    await expect(jwtVerify(a, key, { audience: "payroll" })).rejects.toThrow();
  });

  // PAY-600, R9: public flows use sub=service:web, roles=[], cid=COMPANY_ID, no name or sid.
  it("mints a service:web token without name or sid", async () => {
    const token = await internalToken("identity", serviceClaims());
    const key = await importSPKI(TEST_PUBLIC_PEM, "EdDSA");
    const { payload } = await jwtVerify(token, key);
    expect(payload).toMatchObject({ sub: "service:web", roles: [], cid: COMPANY_ID });
    expect(payload).not.toHaveProperty("name");
    expect(payload).not.toHaveProperty("sid");
  });

  // PAY-600 (build-safe): a missing key throws only on first use.
  it("reads the key lazily", async () => {
    applyEnv({ ...TEST_ENV, INTERNAL_JWT_PRIVATE_KEY: undefined });
    const { resetConfigForTests } = await import("@/lib/config");
    resetConfigForTests();
    await expect(internalToken("payroll", userClaims)).rejects.toThrow(
      /config.invalid: INTERNAL_JWT_PRIVATE_KEY/,
    );
  });
});
