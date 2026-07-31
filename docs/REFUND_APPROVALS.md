# Refund Approvals (HITL)

The refund flow is human-in-the-loop (HITL): the agent never writes a refund
record directly. When a customer's refund is eligible, the agent's
`process_refund` tool stages a `PENDING_APPROVAL` document in the
`refund_requests` Firestore collection instead of executing anything. A
human approver then reviews and approves or rejects the request through the
API/UI described here — only `approve_refund` writes to the `refunds`
collection, and only after passing dual-control and idempotency checks.

This document covers the approver-facing pieces: the API, the minimal
approver UI, and how to bootstrap a user as an approver.

## Bootstrapping an approver

There is no self-service "become an approver" flow and no admin UI for role
management yet — an approver is created by setting a `role` field directly
on a user's document in the `users` collection of the `customer-support-db`
Firestore database.

**Via `gcloud`:**

```bash
gcloud firestore documents update \
  "projects/YOUR_PROJECT_ID/databases/customer-support-db/documents/users/demo-user-002" \
  --update-mask="role" \
  --field="role=approver" \
  --project=YOUR_PROJECT_ID
```

**Via the Firestore console:**

1. Open **Firestore Database** → `customer-support-db` → `users` collection.
2. Open the target user's document (e.g. `demo-user-002`).
3. Add a field named `role` with string value `approver`.
4. Save.

Any authenticated user whose doc has `role == "approver"` will now pass the
`require_approver` dependency on every endpoint below. Removing the field
(or setting it to anything else) revokes approver access immediately — there
is no caching.

## API

All three endpoints require an `Authorization: Bearer <token>` header (the
same token issued by `/api/auth/login`) and are gated by the `require_approver`
FastAPI dependency in `backend/app/main.py`:

- **401** if the request is unauthenticated (no/invalid Authorization header).
- **403** if the request is authenticated but the caller's user doc has no
  `role` field, or a `role` other than `"approver"`.

| Method | Path | Description |
|--------|------|--------------|
| `GET`  | `/api/admin/refunds/pending` | List all `PENDING_APPROVAL` refund requests. |
| `POST` | `/api/admin/refunds/{request_id}/approve` | Approve a request and execute the refund. |
| `POST` | `/api/admin/refunds/{request_id}/reject` | Reject a request. Body: `{"note": str}` (optional). |

`ApprovalError` codes from `backend/app/refund_approvals.py` map to HTTP
status as follows:

| Code | HTTP status | Meaning |
|------|-------------|---------|
| `not_found` | 404 | No `refund_requests` doc with that `request_id`. |
| `not_pending` | 409 | Already approved/rejected/expired — idempotency gate against double-refunding. |
| `self_approval` | 403 | Dual control: the approver cannot be the original requester (approve only). |
| anything else | 400 | Fallback. |

### Example: approve a request

```bash
curl -X POST \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  "https://<backend-url>/api/admin/refunds/REFREQ-abc123/approve"
```

### Example: reject a request with a note

```bash
curl -X POST \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"note": "No evidence of damage provided"}' \
  "https://<backend-url>/api/admin/refunds/REFREQ-abc123/reject"
```

## Approver UI

`frontend/src/components/RefundApprovals.tsx` is a minimal, self-hiding
banner mounted unconditionally in `MainApp.tsx` (there is no `role` field
anywhere in the frontend's auth flow to gate it on client-side). On mount it
calls `GET /api/admin/refunds/pending`:

- **200** → renders a collapsible list of pending requests (order id,
  amount, reason, requested-at) with **Approve** / **Reject** buttons. A
  successful action removes the row immediately and shows a toast; a failed
  action shows an error toast with the backend's `detail` message.
- **401 / 403 / any other failure** → renders nothing at all. This is what
  makes the banner safe to mount for every logged-in user, including
  anonymous and non-approver accounts — the backend's existing
  authorization is the only thing deciding visibility.

## Manual end-to-end verification

1. Set `role: "approver"` on `demo-user-002` as described above.
2. Log in as `demo-user-001` and ask the agent for a refund on a delivered
   order. Expect a "submitted for approval" reply; confirm a
   `refund_requests` doc was created and the `refunds` collection is
   untouched.
3. Log in as `demo-user-002`. The Refund Approvals banner should appear with
   the pending request. Click **Approve**.
4. Confirm: a `refunds` record now exists, the `refund_requests` doc's
   status is `APPROVED` with `approver_id` set, and re-approving (re-click,
   or re-POST the same endpoint) returns 409 with still exactly one refund
   record.
5. As `demo-user-001`, attempt to approve their own request via `curl` —
   expect 403 (`self_approval`).
