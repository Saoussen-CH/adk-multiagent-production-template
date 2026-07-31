import { useState, useEffect, useCallback, CSSProperties } from 'react';
import axios from 'axios';
import { refundApprovalService, RefundApprovalRequest } from '../services/api';
import { useToast } from './Toast';

/**
 * Self-contained, self-hiding approver banner (Task 10 — HITL step 3 UI).
 *
 * There is no `role` field anywhere in the current auth flow (checked
 * AuthContext.tsx's User type and the login/anonymous response shapes), so
 * this component cannot gate its own visibility client-side. Instead it
 * relies entirely on the backend's existing enforcement: it always calls
 * GET /api/admin/refunds/pending on mount, and renders nothing at all if
 * that call fails (401 — unauthenticated/anonymous caller, or 403 — an
 * authenticated caller whose Firestore user doc isn't role: "approver").
 * Only a 200 response, meaning the backend has already confirmed the
 * caller is an approver, causes anything to render. It is therefore safe
 * to mount this unconditionally for every logged-in user (see MainApp.tsx).
 */
export default function RefundApprovals() {
  const { success, error: toastError } = useToast();
  const [loading, setLoading] = useState(true);
  const [visible, setVisible] = useState(false);
  const [collapsed, setCollapsed] = useState(false);
  const [requests, setRequests] = useState<RefundApprovalRequest[]>([]);
  const [rejectingId, setRejectingId] = useState<string | null>(null);
  const [rejectNote, setRejectNote] = useState('');
  const [actionInFlight, setActionInFlight] = useState<string | null>(null);

  const loadPending = useCallback(async () => {
    try {
      const data = await refundApprovalService.getPending();
      setRequests(data.requests);
      setVisible(true);
    } catch {
      // 401 (not authenticated / anonymous) or 403 (authenticated but not
      // an approver) — and, for safety, anything else too. Render nothing
      // rather than surface an error toast to every non-approver on every
      // page load.
      setVisible(false);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadPending();
  }, [loadPending]);

  const handleApprove = async (requestId: string) => {
    setActionInFlight(requestId);
    try {
      await refundApprovalService.approve(requestId);
      setRequests((prev) => prev.filter((r) => r.request_id !== requestId));
      success('Refund approved.');
    } catch (err) {
      toastError(getErrorDetail(err, 'Failed to approve refund.'));
    } finally {
      setActionInFlight(null);
    }
  };

  const startReject = (requestId: string) => {
    setRejectingId(requestId);
    setRejectNote('');
  };

  const cancelReject = () => {
    setRejectingId(null);
    setRejectNote('');
  };

  const confirmReject = async (requestId: string) => {
    setActionInFlight(requestId);
    try {
      await refundApprovalService.reject(requestId, rejectNote);
      setRequests((prev) => prev.filter((r) => r.request_id !== requestId));
      success('Refund rejected.');
      setRejectingId(null);
      setRejectNote('');
    } catch (err) {
      toastError(getErrorDetail(err, 'Failed to reject refund.'));
    } finally {
      setActionInFlight(null);
    }
  };

  // Not an approver, unauthenticated, or still checking — render nothing.
  if (loading || !visible) {
    return null;
  }

  return (
    <div style={styles.container}>
      <button
        type="button"
        onClick={() => setCollapsed((c) => !c)}
        style={styles.header}
        aria-expanded={!collapsed}
      >
        <span style={styles.headerTitle}>
          Refund Approvals
          {requests.length > 0 && (
            <span style={styles.badge}>{requests.length}</span>
          )}
        </span>
        <span style={styles.chevron}>{collapsed ? '▸' : '▾'}</span>
      </button>

      {!collapsed && (
        <div style={styles.body}>
          {requests.length === 0 ? (
            <p style={styles.emptyText}>No pending refund requests.</p>
          ) : (
            <ul style={styles.list}>
              {requests.map((r) => (
                <li key={r.request_id} style={styles.row}>
                  <div style={styles.rowMain}>
                    <div style={styles.rowTitle}>
                      {r.order_id}
                      <span style={styles.amount}>
                        {' '}
                        — ${r.refund_amount.toFixed(2)}
                      </span>
                    </div>
                    <div style={styles.rowMeta}>
                      {r.reason}
                      {r.requested_at && (
                        <> · requested {formatTimestamp(r.requested_at)}</>
                      )}
                    </div>
                  </div>

                  {rejectingId === r.request_id ? (
                    <div style={styles.rejectPanel}>
                      <input
                        type="text"
                        value={rejectNote}
                        onChange={(e) => setRejectNote(e.target.value)}
                        placeholder="Rejection note (optional)"
                        style={styles.input}
                      />
                      <button
                        type="button"
                        onClick={() => confirmReject(r.request_id)}
                        disabled={actionInFlight === r.request_id}
                        style={styles.rejectBtn}
                      >
                        Confirm reject
                      </button>
                      <button
                        type="button"
                        onClick={cancelReject}
                        disabled={actionInFlight === r.request_id}
                        style={styles.cancelBtn}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <div style={styles.actions}>
                      <button
                        type="button"
                        onClick={() => handleApprove(r.request_id)}
                        disabled={actionInFlight === r.request_id}
                        style={styles.approveBtn}
                      >
                        {actionInFlight === r.request_id ? 'Approving…' : 'Approve'}
                      </button>
                      <button
                        type="button"
                        onClick={() => startReject(r.request_id)}
                        disabled={actionInFlight === r.request_id}
                        style={styles.rejectOutlineBtn}
                      >
                        Reject
                      </button>
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString();
}

function getErrorDetail(err: unknown, fallback: string): string {
  if (axios.isAxiosError(err)) {
    const detail = (err.response?.data as { detail?: string } | undefined)?.detail;
    if (typeof detail === 'string' && detail.length > 0) {
      return detail;
    }
  }
  return fallback;
}

const styles: Record<string, CSSProperties> = {
  container: {
    margin: '0.75rem 1rem',
    border: '1px solid #e0e0e0',
    borderRadius: 8,
    background: '#f5f7ff',
    overflow: 'hidden',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    width: '100%',
    padding: '0.6rem 1rem',
    background: 'none',
    border: 'none',
    cursor: 'pointer',
    font: 'inherit',
    color: '#333',
  },
  headerTitle: {
    display: 'flex',
    alignItems: 'center',
    gap: '0.5rem',
    fontWeight: 600,
  },
  badge: {
    display: 'inline-block',
    minWidth: 20,
    padding: '0.05rem 0.4rem',
    borderRadius: 999,
    background: '#667eea',
    color: 'white',
    fontSize: '0.75rem',
    textAlign: 'center',
  },
  chevron: {
    color: '#667eea',
  },
  body: {
    padding: '0 1rem 0.75rem 1rem',
  },
  emptyText: {
    color: '#999',
    fontSize: '0.9rem',
    margin: 0,
  },
  list: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: '0.5rem',
  },
  row: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    flexWrap: 'wrap',
    gap: '0.5rem',
    background: 'white',
    borderRadius: 6,
    padding: '0.6rem 0.75rem',
    border: '1px solid #e8ecff',
  },
  rowMain: {
    minWidth: 0,
  },
  rowTitle: {
    fontWeight: 600,
    color: '#333',
  },
  amount: {
    fontWeight: 400,
    color: '#667eea',
  },
  rowMeta: {
    fontSize: '0.85rem',
    color: '#666',
    marginTop: '0.15rem',
  },
  actions: {
    display: 'flex',
    gap: '0.5rem',
    flexShrink: 0,
  },
  approveBtn: {
    padding: '0.35rem 0.8rem',
    borderRadius: 6,
    border: 'none',
    background: '#667eea',
    color: 'white',
    cursor: 'pointer',
    fontWeight: 500,
  },
  rejectOutlineBtn: {
    padding: '0.35rem 0.8rem',
    borderRadius: 6,
    border: '1px solid #c33',
    background: 'white',
    color: '#c33',
    cursor: 'pointer',
    fontWeight: 500,
  },
  rejectPanel: {
    display: 'flex',
    gap: '0.4rem',
    alignItems: 'center',
    flexWrap: 'wrap',
  },
  input: {
    padding: '0.35rem 0.5rem',
    borderRadius: 6,
    border: '1px solid #ccc',
    minWidth: 180,
  },
  rejectBtn: {
    padding: '0.35rem 0.8rem',
    borderRadius: 6,
    border: 'none',
    background: '#c33',
    color: 'white',
    cursor: 'pointer',
  },
  cancelBtn: {
    padding: '0.35rem 0.8rem',
    borderRadius: 6,
    border: '1px solid #ccc',
    background: 'white',
    color: '#666',
    cursor: 'pointer',
  },
};
