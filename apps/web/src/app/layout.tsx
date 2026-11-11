import type { Metadata } from "next";
import { headers } from "next/headers";
import type { ReactNode } from "react";

import { Providers } from "@/components/providers";
import { WebVitals } from "@/components/web-vitals";

import "./globals.css";

export const metadata: Metadata = {
  title: { default: "PayRun", template: "%s · PayRun" },
};

/**
 * Root layout (web LLD §1.1). Reading the `x-nonce` header (ADR 0009) makes every page dynamic;
 * Next.js puts the nonce from the request's CSP on its own scripts.
 */
export default async function RootLayout({ children }: { children: ReactNode }) {
  // Inline scripts of our own would take this nonce; there are none today (ADR 0009).
  (await headers()).get("x-nonce");
  return (
    <html lang="en">
      <body className="min-h-screen bg-white text-ink antialiased">
        <Providers>
          {children}
          <WebVitals />
        </Providers>
      </body>
    </html>
  );
}
