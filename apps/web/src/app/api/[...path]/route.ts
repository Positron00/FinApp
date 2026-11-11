import { bffError } from "@/lib/errors";
import { route } from "@/lib/route";

/** Any method on an unknown `/api/*` path: 404 `not_found` as JSON with the standard headers (C15). */
const notFound = route({
  name: "/api/[...path]",
  auth: "public",
  ignoreBody: true,
  async handler() {
    throw bffError("not_found");
  },
});

export const GET = notFound;
export const POST = notFound;
export const PUT = notFound;
export const PATCH = notFound;
export const DELETE = notFound;
