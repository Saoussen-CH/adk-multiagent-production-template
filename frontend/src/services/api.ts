import axios, { AxiosError } from 'axios';
import type {
  ChatResponse,
  ChatRequest,
  SessionListResponse,
  MessageHistoryResponse,
} from '../types';

// For development: http://localhost:8000
// For production (Cloud Run): uses relative URLs (served from same origin)
const API_BASE_URL = import.meta.env.VITE_API_URL || '';

// Retry configuration
const MAX_RETRIES = 3;
const RETRY_DELAY_MS = 1000;
const RETRYABLE_STATUS_CODES = [408, 429, 500, 502, 503, 504];

const apiClient = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  timeout: 30000,
});

/**
 * Sleep for specified milliseconds
 */
function sleep(ms: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/**
 * Determine if an error is retryable
 */
function isRetryableError(error: AxiosError): boolean {
  // Network errors are retryable
  if (!error.response) {
    return true;
  }

  // Check if status code is retryable
  return RETRYABLE_STATUS_CODES.includes(error.response.status);
}

/**
 * Get user-friendly error message
 */
function getErrorMessage(error: AxiosError): string {
  if (!error.response) {
    return 'Network error. Please check your connection and try again.';
  }

  const status = error.response.status;
  const data = error.response.data as any;

  // Use server-provided message if available
  if (data?.detail) {
    return data.detail;
  }
  if (data?.error) {
    return data.error;
  }

  // Status-specific messages
  switch (status) {
    case 401:
      return 'Session expired. Please log in again.';
    case 403:
      return 'You don\'t have permission to perform this action.';
    case 404:
      return 'The requested resource was not found.';
    case 408:
    case 504:
      return 'Request timed out. Please try again.';
    case 429:
      return 'Too many requests. Please wait a moment and try again.';
    case 500:
    case 502:
    case 503:
      return 'Server error. Our team has been notified. Please try again later.';
    default:
      return 'An unexpected error occurred. Please try again.';
  }
}

/**
 * Execute request with retry logic
 */
async function withRetry<T>(
  requestFn: () => Promise<T>,
  maxRetries: number = MAX_RETRIES
): Promise<T> {
  let lastError: AxiosError | null = null;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await requestFn();
    } catch (error) {
      if (!axios.isAxiosError(error)) {
        throw error;
      }

      lastError = error;

      // Don't retry if not retryable or last attempt
      if (!isRetryableError(error) || attempt === maxRetries) {
        break;
      }

      // Calculate delay with exponential backoff
      const delay = RETRY_DELAY_MS * Math.pow(2, attempt);
      console.log(`[API] Request failed, retrying in ${delay}ms (attempt ${attempt + 1}/${maxRetries})`);
      await sleep(delay);
    }
  }

  // Throw with user-friendly message
  throw new Error(getErrorMessage(lastError!));
}

// Which merchant/tenant this widget instance is serving.
//
// Every backend endpoint that touches accounts, sessions or commerce data
// requires it — accounts and sessions are stored in the tenant's OWN
// Firestore database, so the backend cannot resolve, authenticate or even
// find anything without it (see backend/app/database.py's module docstring).
// There is no embed-script mechanism in this repo yet for a widget to expose
// its own store identifier, so this falls back to a build-time env var as a
// placeholder for that (out of scope for this plan — see backend
// ChatRequest.tenant_id's docstring).
export function currentTenantId(tenantId?: string): string {
  return tenantId || import.meta.env.VITE_TENANT_ID;
}

// Helper to get auth headers
function getAuthHeaders(): Record<string, string> {
  const token = localStorage.getItem('token');

  if (!token) {
    throw new Error('No active session. Please reload the page.');
  }

  return { Authorization: `Bearer ${token}` };
}

// =============================================================================
// CHAT SERVICE
// =============================================================================

export const chatService = {
  async sendMessage(message: string, sessionId?: string, tenantId?: string): Promise<ChatResponse> {
    const headers = getAuthHeaders();

    console.log('[API] Sending message:', {
      message: message.substring(0, 50),
      session_id: sessionId
    });

    // tenant_id: which merchant/tenant this chat belongs to — see
    // currentTenantId() above.
    const requestData: ChatRequest = {
      message,
      session_id: sessionId,
      tenant_id: currentTenantId(tenantId),
    };

    // Chat requests don't retry on server errors (to avoid duplicate messages)
    // Only retry on network/timeout errors
    const response = await withRetry(
      () => apiClient.post<ChatResponse>('/api/chat', requestData, {
        headers,
        timeout: 150000,  // 2.5 minutes - agent responses can take time
      }),
      1  // Only 1 retry for chat to avoid duplicates
    );

    console.log('[API] Received response:', {
      session_id: response.data.session_id,
      user_id: response.data.user_id
    });

    return response.data;
  },

  async healthCheck(): Promise<{ healthy: boolean; status?: string }> {
    try {
      const response = await apiClient.get('/health', { timeout: 5000 });
      const data = response.data;
      return {
        healthy: response.status === 200 && data.status !== 'unhealthy',
        status: data.status
      };
    } catch {
      return { healthy: false };
    }
  },
};

// =============================================================================
// SESSION SERVICE
// =============================================================================

// =============================================================================
// REFUND APPROVAL SERVICE (Task 10 — HITL approver API)
// =============================================================================

export interface RefundApprovalRequest {
  request_id: string;
  order_id: string;
  user_id: string;
  refund_amount: number;
  reason: string;
  reason_category: string;
  status: string;
  requested_at: string;
  expires_at?: string;
}

export interface RefundApprovalsPendingResponse {
  requests: RefundApprovalRequest[];
}

export interface RefundApprovalActionResponse {
  status: string;
  refund_id?: string;
}

// Deliberately NOT wrapped in withRetry(): the caller (RefundApprovals.tsx)
// needs the raw AxiosError (in particular error.response.status) to decide
// whether to render null (401/403 — not an approver) versus show a toast
// (any other failure). withRetry() replaces thrown errors with a generic
// Error carrying only a friendly message, which would throw away the status
// code this component's self-hiding behavior depends on.
// Every refund-approval call is tenant-scoped: staged refund requests live
// in the requesting tenant's own Firestore database, so the backend needs to
// know which tenant's queue is being addressed before it can find anything.
// currentTenantId() is defined at the top of this module.

export const refundApprovalService = {
  async getPending(tenantId?: string): Promise<RefundApprovalsPendingResponse> {
    const headers = getAuthHeaders();
    const response = await apiClient.get<RefundApprovalsPendingResponse>(
      '/api/admin/refunds/pending',
      { headers, params: { tenant_id: currentTenantId(tenantId) } }
    );
    return response.data;
  },

  async approve(requestId: string, tenantId?: string): Promise<RefundApprovalActionResponse> {
    const headers = getAuthHeaders();
    const response = await apiClient.post<RefundApprovalActionResponse>(
      `/api/admin/refunds/${requestId}/approve`,
      {},
      { headers, params: { tenant_id: currentTenantId(tenantId) } }
    );
    return response.data;
  },

  async reject(requestId: string, note: string, tenantId?: string): Promise<RefundApprovalActionResponse> {
    const headers = getAuthHeaders();
    const response = await apiClient.post<RefundApprovalActionResponse>(
      `/api/admin/refunds/${requestId}/reject`,
      { note },
      { headers, params: { tenant_id: currentTenantId(tenantId) } }
    );
    return response.data;
  },
};

// Sessions are stored in their tenant's own Firestore database and the auth
// token is verified against that same database, so tenant_id is a REQUIRED
// query parameter on every call below — omitting it is a 422, not a default.
export const sessionService = {
  async listSessions(tenantId?: string): Promise<SessionListResponse> {
    const headers = getAuthHeaders();
    const response = await withRetry(
      () => apiClient.get<SessionListResponse>('/api/sessions', {
        headers,
        params: { tenant_id: currentTenantId(tenantId) },
      })
    );
    return response.data;
  },

  async renameSession(sessionId: string, newName: string, tenantId?: string): Promise<void> {
    const headers = getAuthHeaders();
    await withRetry(
      () => apiClient.put(
        `/api/sessions/${sessionId}/rename`,
        { session_name: newName },
        { headers, params: { tenant_id: currentTenantId(tenantId) } }
      )
    );
  },

  async deleteSession(sessionId: string, tenantId?: string): Promise<void> {
    const headers = getAuthHeaders();
    await withRetry(
      () => apiClient.delete(`/api/sessions/${sessionId}`, {
        headers,
        params: { tenant_id: currentTenantId(tenantId) },
      })
    );
  },

  async getSessionMessages(sessionId: string, tenantId?: string): Promise<MessageHistoryResponse> {
    const headers = getAuthHeaders();
    const response = await withRetry(
      () => apiClient.get<MessageHistoryResponse>(
        `/api/sessions/${sessionId}/messages`,
        { headers, params: { tenant_id: currentTenantId(tenantId) } }
      )
    );
    return response.data;
  },
};
