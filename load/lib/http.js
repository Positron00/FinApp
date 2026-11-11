// Request helpers shared by the k6 scripts (web LLD §9.3): every request is tagged with `name`
// (the route template) and `kind` (simple | heavy); mutations send Origin: BASE_URL (C12).
import http from "k6/http";

import { BASE_URL, TARGET_URL } from "./session.js";

export function params(cookie, name, kind = "simple", extra = {}) {
  return {
    headers: {
      Cookie: cookie,
      Accept: "application/json",
      "Content-Type": "application/json",
      Origin: BASE_URL,
      ...(extra.headers || {}),
    },
    tags: { name, kind, ...(extra.tags || {}) },
  };
}

export const get = (path, p) => http.get(`${TARGET_URL}${path}`, p);
export const post = (path, body, p) =>
  http.post(`${TARGET_URL}${path}`, body === null ? null : JSON.stringify(body), p);
export const put = (path, body, p) => http.put(`${TARGET_URL}${path}`, JSON.stringify(body), p);
export const patch = (path, body, p) => http.patch(`${TARGET_URL}${path}`, JSON.stringify(body), p);
export const del = (path, p) => http.del(`${TARGET_URL}${path}`, null, p);

/** "YYYY-MM-DD" for a UTC date. */
export const isoDate = (d) => d.toISOString().slice(0, 10);

/** A random biweekly period start far from any real run: anchor + 14 k days. */
export function scratchBiweeklyStart(anchor) {
  const k = 3000 + Math.floor(Math.random() * 30000);
  return isoDate(new Date(Date.parse(`${anchor}T00:00:00Z`) + k * 14 * 86400000));
}
