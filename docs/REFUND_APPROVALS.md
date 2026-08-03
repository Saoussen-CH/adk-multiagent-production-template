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
| `GET`  | `/api/admin/refunds/pending?tenant_id=<id>` | List that tenant's `PENDING_APPROVAL` refund requests. |
| `POST` | `/api/admin/refunds/{request_id}/approve?tenant_id=<id>` | Approve a request and execute the refund. |
| `POST` | `/api/admin/refunds/{request_id}/reject?tenant_id=<id>` | Reject a request. Body: `{"note": str}` (optional). |

### `tenant_id` is required on all three

There is no default tenant. `process_refund` stages its `PENDING_APPROVAL`
document into the **requesting tenant's own Firestore database** (the one
behind `get_provider(tenant_id)._db`), so the API cannot even locate a
request without knowing which tenant it belongs to. It is a query parameter
rather than something inferred from the `request_id`, because the document
has to be found before it can be read — and for `pending` there is no
document to infer from at all.

The supplied `tenant_id` is then matched against each document's own
`tenant_id` field, so acting on another tenant's `request_id` is reported as
`404` (not a distinct error — an approver must not be able to probe whether
an id exists under a different tenant) and never results in a cross-tenant
write.

Two extra failure modes come from tenant resolution itself:

| HTTP status | Meaning |
|-------------|---------|
| `404` | Unknown `tenant_id` — no `tenants/{tenant_id}` document exists. |
| `501` | The tenant's provider has no refund-request store of its own (a Shopify-backed tenant). Refund staging is this product's workflow layer, not something Shopify hosts. |

> **Known gap:** user documents carry no `tenant_id` today, so
> `require_approver_for_tenant` can only enforce the tenant match for users
> that have one — any approver-role user can still address any tenant's
> queue. Closing this needs a tenant-membership model on users (or
> per-tenant approver roles); the check is already written so that adding
> the field to user docs is a data change rather than a code change.

`ApprovalError` codes from `backend/app/refund_approvals.py` map to HTTP
status as follows:

| Code | HTTP status | Meaning |
|------|-------------|---------|
| `not_found` | 404 | No `refund_requests` doc with that `request_id` **for that tenant**. |
| `not_pending` | 409 | Already approved/rejected/expired — idempotency gate against double-refunding. |
| `self_approval` | 403 | Dual control: the approver cannot be the original requester (approve only). |
| anything else | 400 | Fallback. |

### Example: approve a request

```bash
curl -X POST \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  "https://<backend-url>/api/admin/refunds/REFREQ-abc123/approve?tenant_id=acme-electronics"
```

### Example: reject a request with a note

```bash
curl -X POST \
  -H "Authorization: Bearer $APPROVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"note": "No evidence of damage provided"}' \
  "https://<backend-url>/api/admin/refunds/REFREQ-abc123/reject?tenant_id=acme-electronics"
```

## Approver UI

`frontend/src/components/RefundApprovals.tsx` is a minimal, self-hiding
banner mounted unconditionally in `MainApp.tsx` (there is no `role` field
anywhere in the frontend's auth flow to gate it on client-side). On mount it
calls `GET /api/admin/refunds/pending` (passing the same `tenant_id` the chat
widget uses — `VITE_TENANT_ID`, see `frontend/src/services/api.ts`):

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
