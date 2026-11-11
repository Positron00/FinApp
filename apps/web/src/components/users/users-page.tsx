"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type FormEvent } from "react";
import { flushSync } from "react-dom";

import { CursorPager, useCursorStack } from "@/components/cursor-pager";
import { ConfirmDialog, Dialog } from "@/components/dialog";
import { ErrorBanner } from "@/components/error-banner";
import { FormError, TextField } from "@/components/field";
import { ApiError, apiFetch } from "@/lib/client/api";
import {
  apiFieldErrors,
  errorText,
  focusFirstInvalid,
  zodFieldErrors,
  type FieldErrors,
} from "@/lib/client/form-errors";
import { listQuery, queryKeys, type PageOf } from "@/lib/client/query-keys";
import { ROLES, type Role } from "@/lib/roles";
import { InviteBody } from "@/lib/schemas/requests";
import type { PendingInvite, User } from "@/lib/schemas/responses";

export const ROLE_LABELS: Record<Role, string> = {
  admin: "Admin",
  payroll_admin: "Payroll admin",
  approver: "Approver",
};

const STATUS_LABELS: Record<User["status"], string> = {
  pending: "Pending",
  active: "Active",
  inactive: "Inactive",
};

type Action =
  { kind: "invite" } | { kind: "roles"; user: User } | { kind: "deactivate"; user: User };

/**
 * `/users` (web LLD §6.2; PAY-101, PAY-102, PAY-108): admin. Invite, edit roles, deactivate (a
 * confirm) and resend (by `pending_invite_id`; R21, C11). There's no "Reactivate": inactive
 * users are read-only (R37, C25). The admin's own row has its controls disabled
 * (`cannot_modify_self`). A role change or deactivation ends the user's sessions at once (PAY-107).
 */
export function UsersPage({ meId }: { meId: string }) {
  const qc = useQueryClient();
  const pager = useCursorStack();
  const [action, setAction] = useState<Action | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const q = useQuery({
    queryKey: queryKeys.users(pager.cursor),
    queryFn: () => apiFetch<PageOf<User>>(`/api/users?${listQuery(50, pager.cursor)}`),
  });

  const resend = useMutation({
    mutationFn: (inviteId: string) =>
      apiFetch<PendingInvite>(`/api/invites/${inviteId}/resend`, { method: "POST" }),
    retry: 0,
    onSuccess: () => {
      setNotice("Invite sent again.");
      void qc.invalidateQueries({ queryKey: ["users"] });
    },
    onError: () => setNotice(null),
  });

  function done(message: string) {
    setAction(null);
    setNotice(message);
    void qc.invalidateQueries({ queryKey: ["users"] });
  }

  return (
    <>
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold">Users</h1>
        <button
          type="button"
          className="rounded bg-brand px-4 py-2 text-white"
          onClick={() => setAction({ kind: "invite" })}
        >
          Invite user
        </button>
      </div>
      {notice ? (
        <p role="status" className="mt-3">
          {notice}
        </p>
      ) : null}
      <FormError
        message={resend.error ? errorText(resend.error) : null}
        requestId={resend.error instanceof ApiError ? resend.error.requestId : null}
      />
      {q.isError ? <ErrorBanner error={q.error} onRetry={() => void q.refetch()} /> : null}
      {q.isPending ? <p className="mt-4">Loading…</p> : null}
      {q.data ? (
        <>
          <table className="mt-4 w-full text-left">
            <caption className="sr-only">Users</caption>
            <thead>
              <tr>
                <th scope="col">Name</th>
                <th scope="col">Email</th>
                <th scope="col">Roles</th>
                <th scope="col">Status</th>
                <th scope="col">Actions</th>
              </tr>
            </thead>
            <tbody>
              {q.data.items.map((u) => {
                const self = u.id === meId;
                return (
                  <tr key={u.id} className="border-t">
                    <td>
                      {u.name}
                      {self ? " (you)" : ""}
                    </td>
                    <td>{u.email}</td>
                    <td>{u.roles.map((r) => ROLE_LABELS[r]).join(", ")}</td>
                    <td>{STATUS_LABELS[u.status]}</td>
                    <td className="flex flex-wrap gap-2 py-1">
                      {u.status !== "inactive" ? (
                        <button
                          type="button"
                          className="rounded border px-2 py-1 disabled:opacity-50"
                          disabled={self}
                          aria-label={`Edit roles for ${u.name}`}
                          onClick={() => setAction({ kind: "roles", user: u })}
                        >
                          Edit roles
                        </button>
                      ) : null}
                      {u.status === "active" ? (
                        <button
                          type="button"
                          className="rounded border border-danger px-2 py-1 text-danger disabled:opacity-50"
                          disabled={self}
                          aria-label={`Deactivate ${u.name}`}
                          onClick={() => setAction({ kind: "deactivate", user: u })}
                        >
                          Deactivate
                        </button>
                      ) : null}
                      {u.status === "pending" && u.pending_invite_id ? (
                        <button
                          type="button"
                          className="rounded border px-2 py-1 disabled:opacity-50"
                          disabled={resend.isPending}
                          aria-label={`Resend invite to ${u.name}`}
                          onClick={() => resend.mutate(u.pending_invite_id!)}
                        >
                          Resend invite
                        </button>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <CursorPager
            nextCursor={q.data.next_cursor}
            hasPrevious={pager.hasPrevious}
            onNext={pager.next}
            onPrevious={pager.previous}
          />
        </>
      ) : null}

      {action?.kind === "invite" ? (
        <InviteDialog
          onClose={() => setAction(null)}
          onDone={(u) => done(`Invite sent to ${u.email}.`)}
        />
      ) : null}
      {action?.kind === "roles" ? (
        <UserEditDialog
          user={action.user}
          onClose={() => setAction(null)}
          onDone={() =>
            done(`Roles updated for ${action.user.name}. They'll need to sign in again.`)
          }
        />
      ) : null}
      {action?.kind === "deactivate" ? (
        <DeactivateDialog
          user={action.user}
          onClose={() => setAction(null)}
          onDone={() => done(`${action.user.name} was deactivated.`)}
        />
      ) : null}
    </>
  );
}

/** Role checkboxes, at least one (PAY-101, PAY-108). */
function RoleCheckboxes({
  value,
  onChange,
  error,
}: {
  value: Role[];
  onChange: (r: Role[]) => void;
  error?: string;
}) {
  return (
    <fieldset
      className="flex flex-col gap-1"
      aria-invalid={error ? true : undefined}
      aria-describedby={error ? "roles-error" : undefined}
    >
      <legend>Roles</legend>
      {ROLES.map((r) => (
        <label key={r} className="flex items-center gap-2">
          <input
            type="checkbox"
            name="roles"
            value={r}
            checked={value.includes(r)}
            onChange={(e) =>
              onChange(e.target.checked ? [...value, r] : value.filter((x) => x !== r))
            }
          />
          {ROLE_LABELS[r]}
        </label>
      ))}
      {error ? (
        <span id="roles-error" className="text-danger">
          {error}
        </span>
      ) : null}
    </fieldset>
  );
}

const ROLES_REQUIRED = "Choose at least one role.";

export function InviteDialog({
  onClose,
  onDone,
}: {
  onClose: () => void;
  onDone: (u: User) => void;
}) {
  const [email, setEmail] = useState("");
  const [name, setName] = useState("");
  const [roles, setRoles] = useState<Role[]>([]);
  const [errors, setErrors] = useState<FieldErrors>({});
  const formRef = useRef<HTMLFormElement>(null);
  const invite = useMutation({
    mutationFn: (body: InviteBody) => apiFetch<User>("/api/users/invite", { method: "POST", body }),
    retry: 0,
    onSuccess: onDone,
    onError: (err) => {
      // Commit the errors before focusing: aria-invalid must be in the DOM (PAY-NFR-14).
      flushSync(() =>
        setErrors(
          err instanceof ApiError && err.code === "user_email_taken"
            ? { email: err.message }
            : apiFieldErrors(err),
        ),
      );
      focusFirstInvalid(formRef.current);
    },
  });

  function submit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const parsed = InviteBody.safeParse({ email, name, roles });
    if (!parsed.success) {
      flushSync(() =>
        setErrors(
          zodFieldErrors(parsed.error, {
            email: "Enter a valid email address.",
            roles: ROLES_REQUIRED,
            name: "Enter a name.",
          }),
        ),
      );
      focusFirstInvalid(formRef.current);
      return;
    }
    setErrors({});
    invite.mutate(parsed.data);
  }

  const err = invite.error;
  return (
    <Dialog title="Invite user" onClose={onClose}>
      <form ref={formRef} className="mt-3 flex flex-col gap-4" onSubmit={submit} noValidate>
        <TextField
          label="Email"
          name="email"
          type="email"
          value={email}
          onChange={setEmail}
          error={errors.email}
        />
        <TextField label="Name" name="name" value={name} onChange={setName} error={errors.name} />
        <RoleCheckboxes value={roles} onChange={setRoles} error={errors.roles} />
        <FormError
          message={
            err && !(err instanceof ApiError && err.code === "user_email_taken")
              ? errorText(err)
              : null
          }
          requestId={err instanceof ApiError ? err.requestId : null}
        />
        <div className="flex gap-2">
          <button
            type="submit"
            className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
            disabled={invite.isPending}
          >
            Send invite
          </button>
          <button type="button" className="rounded border px-4 py-2" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

export function UserEditDialog({
  user,
  onClose,
  onDone,
}: {
  user: User;
  onClose: () => void;
  onDone: (u: User) => void;
}) {
  const [roles, setRoles] = useState<Role[]>(user.roles);
  const [roleError, setRoleError] = useState<string | undefined>();
  const patch = useMutation({
    mutationFn: (r: Role[]) =>
      apiFetch<User>(`/api/users/${user.id}`, { method: "PATCH", body: { roles: r } }),
    retry: 0,
    onSuccess: onDone,
  });
  const err = patch.error;
  return (
    <Dialog title={`Edit roles for ${user.name}`} onClose={onClose}>
      <form
        className="mt-3 flex flex-col gap-4"
        noValidate
        onSubmit={(e) => {
          e.preventDefault();
          if (roles.length === 0) {
            setRoleError(ROLES_REQUIRED);
            return;
          }
          setRoleError(undefined);
          patch.mutate(roles);
        }}
      >
        <RoleCheckboxes value={roles} onChange={setRoles} error={roleError} />
        <p className="text-sm text-muted">Changing roles signs this user out everywhere.</p>
        <FormError
          message={err ? errorText(err) : null}
          requestId={err instanceof ApiError ? err.requestId : null}
        />
        <div className="flex gap-2">
          <button
            type="submit"
            className="rounded bg-brand px-4 py-2 text-white disabled:opacity-50"
            disabled={patch.isPending}
          >
            Save roles
          </button>
          <button type="button" className="rounded border px-4 py-2" onClick={onClose}>
            Cancel
          </button>
        </div>
      </form>
    </Dialog>
  );
}

function DeactivateDialog({
  user,
  onClose,
  onDone,
}: {
  user: User;
  onClose: () => void;
  onDone: () => void;
}) {
  const patch = useMutation({
    mutationFn: () =>
      apiFetch<User>(`/api/users/${user.id}`, { method: "PATCH", body: { status: "inactive" } }),
    retry: 0,
    onSuccess: onDone,
  });
  const err = patch.error;
  return (
    <ConfirmDialog
      title={`Deactivate ${user.name}?`}
      confirmLabel="Deactivate"
      pending={patch.isPending}
      onConfirm={() => patch.mutate()}
      onClose={onClose}
    >
      <p>
        They&apos;re signed out at once and can&apos;t sign in again. This can&apos;t be undone in
        this version.
      </p>
      <FormError
        message={err ? errorText(err) : null}
        requestId={err instanceof ApiError ? err.requestId : null}
      />
    </ConfirmDialog>
  );
}
