"use client";

export default function GlobalError({ error }: { error: Error & { digest?: string } }) {
  return (
    <html lang="en">
      <body>
        <main id="main" style={{ padding: "2rem", fontFamily: "system-ui, sans-serif" }}>
          <h1>Something went wrong</h1>
          <p role="alert">
            Please reload the page.{error.digest ? ` Reference: ${error.digest}.` : ""}
          </p>
        </main>
      </body>
    </html>
  );
}
