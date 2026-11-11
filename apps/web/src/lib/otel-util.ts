/** `^<origin>/` for a base URL, escaped: the propagateContextUrls entries (web LLD §7.4). */
export function originRegex(baseUrl: string): RegExp {
  const origin = new URL(baseUrl).origin;
  return new RegExp(`^${origin.replace(/[.*+?^${}()|[\]\\/]/g, "\\$&")}/`);
}
