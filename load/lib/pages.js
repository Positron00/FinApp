// Cursor paging (web LLD §9.3; C8): follow next_cursor with limit=100 until it is null and return
// every item. Each page request is tagged with the route template.
import http from "k6/http";
import { check } from "k6";

import { TARGET_URL } from "./session.js";

export function allPages(path, params) {
  const items = [];
  let cursor = null;
  for (let n = 0; n < 1000; n++) {
    const sep = path.includes("?") ? "&" : "?";
    const url = `${TARGET_URL}${path}${sep}limit=100${cursor ? `&cursor=${cursor}` : ""}`;
    const r = http.get(url, params);
    if (!check(r, { [`${params.tags.name} page 200`]: (x) => x.status === 200 })) return items;
    const body = r.json();
    items.push(...body.items);
    cursor = body.next_cursor;
    if (!cursor) return items;
  }
  throw new Error(`more than 1000 pages: ${path}`);
}
