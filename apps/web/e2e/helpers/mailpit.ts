import { expect } from "@playwright/test";

import { mailpitUrl } from "./env";

/**
 * Invite and reset tokens from Mailpit (web LLD §9.2; PAY-101, PAY-103, PAY-105). Never
 * hard-coded: every token comes from the email the stack actually sent.
 *
 * 1. Poll `GET /api/v1/search?query=to:"<email>"` (every 250 ms, up to 15 s; newest first).
 * 2. `GET /api/v1/message/{ID}` and match the `Text` body against `/(accept|reset)#token=…`.
 * 3. Return the token and the full link, which tests open exactly as a user would.
 *
 * `after` skips messages sent before a point in time (a resend or a second reset).
 */
export interface MailToken {
  token: string;
  link: string;
}

interface SearchResult {
  messages?: { ID: string; Created: string }[];
}

export async function latestToken(
  email: string,
  kind: "accept" | "reset",
  after?: Date,
): Promise<MailToken> {
  const pattern = new RegExp(`(https?://[^\\s"'<>]+/${kind}#token=([A-Za-z0-9_-]+))`);
  const box: { hit?: MailToken } = {};
  await expect
    .poll(
      async () => {
        const q = encodeURIComponent(`to:"${email}"`);
        const res = await fetch(`${mailpitUrl()}/api/v1/search?query=${q}&limit=10`);
        if (!res.ok) return null;
        const body = (await res.json()) as SearchResult;
        for (const m of body.messages ?? []) {
          if (after && new Date(m.Created).getTime() < after.getTime() - 1000) continue;
          const detail = (await (await fetch(`${mailpitUrl()}/api/v1/message/${m.ID}`)).json()) as {
            Text?: string;
            HTML?: string;
          };
          const hit = pattern.exec(detail.Text ?? "") ?? pattern.exec(detail.HTML ?? "");
          if (hit?.[1] && hit[2]) {
            box.hit = { link: hit[1], token: hit[2] };
            return box.hit.token;
          }
        }
        return null;
      },
      { message: `a ${kind} email to ${email} in Mailpit`, timeout: 15_000, intervals: [250] },
    )
    .not.toBeNull();
  if (!box.hit) throw new Error(`no ${kind} email to ${email}`);
  return box.hit;
}
