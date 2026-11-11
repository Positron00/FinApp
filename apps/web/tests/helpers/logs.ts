import { setLogSinkForTests } from "@/lib/logger";

/** Captures every log line the server modules write (as parsed JSON and raw text). */
export class LogCapture {
  readonly raw: string[] = [];

  write(line: string): void {
    this.raw.push(line);
  }

  get lines(): Record<string, unknown>[] {
    return this.raw.map((l) => JSON.parse(l) as Record<string, unknown>);
  }

  events(name: string): Record<string, unknown>[] {
    return this.lines.filter((l) => l.event === name);
  }

  text(): string {
    return this.raw.join("\n");
  }
}

export function captureLogs(): LogCapture {
  const cap = new LogCapture();
  setLogSinkForTests(cap);
  return cap;
}
