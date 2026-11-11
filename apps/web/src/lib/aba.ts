/**
 * The ABA routing-number checksum (web LLD §1.3), for instant form feedback; payroll stays
 * authoritative (PAY-201). Shared (server and browser). Integer arithmetic only.
 *
 * 3 × (d1 + d4 + d7) + 7 × (d2 + d5 + d8) + (d3 + d6 + d9) ≡ 0 (mod 10).
 */
const WEIGHTS = [3, 7, 1, 3, 7, 1, 3, 7, 1] as const;

export function abaChecksumOk(routing: string): boolean {
  if (!/^\d{9}$/.test(routing)) return false;
  let sum = 0;
  for (let i = 0; i < 9; i++) sum += WEIGHTS[i]! * (routing.charCodeAt(i) - 48);
  return sum % 10 === 0;
}
