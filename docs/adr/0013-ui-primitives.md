# 0013. Local accessible UI primitives instead of shadcn/ui for 0.1.0

Status: accepted 2026-11-11
Context:
- The skill's default stack lists Tailwind plus shadcn/ui (Radix primitives).
- F5 needed dialogs, tabs and form fields. Adding shadcn/Radix and react-hook-form would add about a dozen packages, change the lead-owned pnpm lock, and pull in versions that the two-week `minimumReleaseAge` rule would hold back mid-task.
- Accessibility is a requirement (PAY-NFR-14: WAI-ARIA tabs, focus-trapped dialogs, labelled fields, live regions).
Options:
1. **shadcn/ui and Radix** (the default). Well-tested accessibility, more code to vendor, a larger dependency surface.
2. **Small local components:**
   - `apps/web/src/components/dialog.tsx`, `tabs.tsx` and `field.tsx` (in `components/`, not a `ui/` subfolder), built to the WAI-ARIA Authoring Practices: focus trap and return, roving tabIndex with arrow, Home and End keys, and `aria-describedby` and `aria-invalid` on fields.
   - Each one has a behaviour test.
   - Tailwind for styling.
Recommendation: option 2 for 0.1.0. It meets PAY-NFR-14 with fewer dependencies. The axe check in e2e (F6) and the keyboard tests are the evidence.
Decision: option 2, by the lead on 2026-11-11, within the skill's rule that stack deviations need an ADR.
Consequences:
- The primitives are maintained in-repo.
- Revisit if the UI grows beyond about 10 dialog or tab uses, or if axe or keyboard tests start needing workarounds. In that case, adopt shadcn/ui and delete the local components.
