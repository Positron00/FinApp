"use client";

import { TextField } from "@/components/field";
import { abaChecksumOk } from "@/lib/aba";

export interface BankInput {
  bank_routing: string;
  bank_account: string;
}

export const EMPTY_BANK: BankInput = { bank_routing: "", bank_account: "" };

/**
 * Client-side bank checks (PAY-201): 9 digits with the ABA checksum (instant feedback; payroll
 * is authoritative) and an account of 4–17 digits. Returns per-field messages.
 */
export function bankErrors(b: BankInput): Partial<Record<keyof BankInput, string>> {
  const out: Partial<Record<keyof BankInput, string>> = {};
  if (!/^\d{9}$/.test(b.bank_routing)) out.bank_routing = "Enter the 9-digit routing number.";
  else if (!abaChecksumOk(b.bank_routing)) {
    out.bank_routing = "This routing number isn't valid. Check the 9 digits.";
  }
  if (!/^\d{4,17}$/.test(b.bank_account)) out.bank_account = "Enter 4 to 17 digits.";
  return out;
}

/**
 * The routing and account inputs. They're never prefilled: the existing numbers are never
 * shown or sent to the browser, only `••••last4` (ADR 0004, PAY-NFR-08).
 */
export function BankFields({
  value,
  onChange,
  errors,
}: {
  value: BankInput;
  onChange: (v: BankInput) => void;
  errors: Partial<Record<keyof BankInput, string>>;
}) {
  return (
    <>
      <TextField
        label="Routing number"
        name="bank_routing"
        inputMode="numeric"
        autoComplete="off"
        value={value.bank_routing}
        onChange={(v) => onChange({ ...value, bank_routing: v.trim() })}
        error={
          errors.bank_routing ??
          (value.bank_routing.length === 9 && !abaChecksumOk(value.bank_routing)
            ? "This routing number isn't valid. Check the 9 digits."
            : undefined)
        }
      />
      <TextField
        label="Account number"
        name="bank_account"
        inputMode="numeric"
        autoComplete="off"
        value={value.bank_account}
        onChange={(v) => onChange({ ...value, bank_account: v.trim() })}
        error={errors.bank_account}
        hint="4 to 17 digits."
      />
    </>
  );
}
