import { abaChecksumOk } from "@/lib/aba";
import {
  bpsToInput,
  centsToInput,
  formatCents,
  formatHours,
  hoursToInput,
  parseHours,
  parseMoney,
  parsePercent,
} from "@/lib/money";

/** ADR 0006: string-integer conversions, no float anywhere on the path. */
describe("parseMoney", () => {
  // ADR 0006, PAY-201: `^\d{1,8}(\.\d{1,2})?$` → cents.
  it.each([
    ["0", 0],
    ["1", 100],
    ["1.5", 150],
    ["1.05", 105],
    [" 1234.56 ", 123_456],
    ["99999999.99", 9_999_999_999],
    ["0.1", 10],
  ])("%j → %i", (text, cents) => {
    expect(parseMoney(text)).toBe(cents);
  });

  // ADR 0006: anything else is refused.
  it.each(["", ".5", "1.", "1.234", "-1", "1e3", "1,000", "123456789", "abc", "0x10"])(
    "refuses %j",
    (text) => {
      expect(parseMoney(text)).toBeNull();
    },
  );

  // ADR 0006: exact against BigInt arithmetic over a sweep of values (no float rounding).
  it("matches BigInt arithmetic", () => {
    for (let i = 0; i < 2000; i++) {
      const whole = BigInt(Math.floor(Math.random() * 100_000_000));
      const frac = BigInt(i % 100);
      const text = `${whole}.${String(frac).padStart(2, "0")}`;
      expect(BigInt(parseMoney(text)!)).toBe(whole * 100n + frac);
      expect(parseMoney(centsToInput(parseMoney(text)))).toBe(parseMoney(text));
    }
  });
});

describe("parsePercent and parseHours", () => {
  // PAY-200: percent → basis points.
  it("parses percent", () => {
    expect(parsePercent("20")).toBe(2000);
    expect(parsePercent("7.65")).toBe(765);
    expect(parsePercent("0.5")).toBe(50);
    expect(parsePercent("99.99")).toBe(9999);
    expect(parsePercent("100")).toBeNull();
    expect(parsePercent("1.234")).toBeNull();
    expect(parsePercent("")).toBeNull();
  });

  // PAY-301: hours → hundredths.
  it("parses hours", () => {
    expect(parseHours("80")).toBe(8000);
    expect(parseHours("80.5")).toBe(8050);
    expect(parseHours("744")).toBe(74_400);
    expect(parseHours("999.99")).toBe(99_999);
    expect(parseHours("1000")).toBeNull();
    expect(parseHours("-1")).toBeNull();
  });
});

describe("input and display formatting", () => {
  // ADR 0006: round-trips between integers and input text.
  it("round-trips", () => {
    expect(centsToInput(123_456)).toBe("1234.56");
    expect(centsToInput(5)).toBe("0.05");
    expect(centsToInput(null)).toBe("");
    expect(centsToInput(undefined)).toBe("");
    expect(bpsToInput(2000)).toBe("20.00");
    expect(bpsToInput(765)).toBe("7.65");
    expect(parsePercent(bpsToInput(765))).toBe(765);
    expect(hoursToInput(8050)).toBe("80.50");
    expect(hoursToInput(null)).toBe("");
    expect(parseHours(hoursToInput(1))).toBe(1);
    expect(formatHours(8050)).toBe("80.50");
    expect(formatHours(0)).toBe("0.00");
    expect(formatHours(null)).toBe("—");
    expect(formatCents(parseMoney("1234.5"))).toBe("$1,234.50");
  });
});

describe("abaChecksumOk", () => {
  // PAY-201: the ABA routing checksum (payroll stays authoritative).
  it.each(["021000021", "011000015", "123456780", "026009593", "111000025"])("accepts %s", (r) => {
    expect(abaChecksumOk(r)).toBe(true);
  });

  // PAY-201: a wrong check digit, the wrong length or non-digits.
  it.each(["021000022", "123456789", "12345678", "0210000210", "02100002a", ""])(
    "refuses %j",
    (r) => {
      expect(abaChecksumOk(r)).toBe(false);
    },
  );
});
