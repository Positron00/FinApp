import { z } from "zod";

import { ROLES } from "../roles";

/** Shared building blocks (web LLD §2.3, §2.4). Shared with the browser: no server imports. */
export const Uuid = z.uuid();
export const IsoDate = z.string().regex(/^\d{4}-\d{2}-\d{2}$/);
export const DateTime = z.iso.datetime({ offset: true });
export const Cents = z.number().int();
export const Email = z.string().trim().toLowerCase().pipe(z.email().max(254));
export const Password = z.string().min(12).max(256);
export const Cursor = z
  .string()
  .max(512)
  .regex(/^[A-Za-z0-9_-]+$/)
  .optional();
export const Limit = z.coerce.number().int().min(1).max(100).default(50);
export const RoleEnum = z.enum(ROLES);
export const Schedule = z.enum(["biweekly", "monthly"]);
export const PayType = z.enum(["salaried", "hourly"]);
