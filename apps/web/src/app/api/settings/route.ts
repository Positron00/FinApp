import { callService } from "@/lib/api/call";
import { json, methodNotAllowed, route } from "@/lib/route";
import { SettingsBody } from "@/lib/schemas/requests";
import { Settings } from "@/lib/schemas/responses";

const NAME = "/api/settings";
const ALLOWED = ["GET", "PUT"];

/** GET /api/settings (PAY-200): any role. 404 `company_settings_not_found` before setup. */
export const GET = route({
  name: NAME,
  auth: {},
  async handler({ payroll }) {
    const settings = await callService(
      "payroll",
      NAME,
      payroll.GET("/v1/company/settings"),
      Settings,
    );
    return json(settings);
  },
});

/**
 * PUT /api/settings (PAY-200): admin. 409 `anchor_locked` once biweekly runs exist (R33);
 * 422 for out-of-range rates, an unknown time zone or a non-Monday anchor.
 */
export const PUT = route({
  name: NAME,
  auth: { roles: ["admin"] },
  body: SettingsBody,
  async handler({ body, payroll }) {
    const settings = await callService(
      "payroll",
      NAME,
      payroll.PUT("/v1/company/settings", { body }),
      Settings,
    );
    return json(settings);
  },
});

export const POST = methodNotAllowed(NAME, ALLOWED);
export const PATCH = methodNotAllowed(NAME, ALLOWED);
export const DELETE = methodNotAllowed(NAME, ALLOWED);
