import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import type { User, AuthResponse, AnonymousUserResponse } from '../types';
import { currentTenantId } from '../services/api';

interface AuthContextType {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  authError: string | null;
  showLoginScreen: boolean;
  openLoginScreen: () => void;
  closeLoginScreen: () => void;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, name: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  retryAnonymousSession: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

const ANONYMOUS_SESSION_ERROR = 'Could not connect. Please check your connection and try again.';

async function createAnonymousSession(): Promise<{ user: User; token: string }> {
  const response = await fetch('/api/auth/anonymous', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tenant_id: currentTenantId() }),
  });

  if (!response.ok) {
    throw new Error('Failed to start a chat session');
  }

  const data: AnonymousUserResponse = await response.json();
  return {
    user: { user_id: data.user_id, is_anonymous: true },
    token: data.token,
  };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [authError, setAuthError] = useState<string | null>(null);
  const [showLoginScreen, setShowLoginScreen] = useState(false);

  // Shared by the startup effect, logout, and the manual retry action: try
  // to establish a fresh anonymous session, updating state/localStorage on
  // success or surfacing a visible, recoverable authError on failure. Never
  // leaves user/token in a state that isn't reflected by either a real user
  // or an authError — that invariant is what keeps MainApp from ever having
  // to render a dead end.
  const establishAnonymousSession = async (): Promise<void> => {
    try {
      const { user: anonUser, token: anonToken } = await createAnonymousSession();
      setUser(anonUser);
      setToken(anonToken);
      setAuthError(null);
      localStorage.setItem('user', JSON.stringify(anonUser));
      localStorage.setItem('token', anonToken);
    } catch (error) {
      console.error('Failed to start anonymous session:', error);
      setAuthError(ANONYMOUS_SESSION_ERROR);
    }
  };

  // On load: use a stored session if there is one, otherwise silently start
  // an anonymous one. There is no visible gate — the visitor always lands
  // in a ready chat, whether that's a returning session or a brand-new
  // anonymous one. If the silent attempt fails (backend down, network
  // error), authError is set so MainApp can show a recoverable error state
  // instead of a blank page.
  useEffect(() => {
    const storedUser = localStorage.getItem('user');
    const storedToken = localStorage.getItem('token');

    if (storedUser && storedToken) {
      try {
        setUser(JSON.parse(storedUser));
        setToken(storedToken);
        setIsLoading(false);
        return;
      } catch (error) {
        console.error('Failed to parse stored user:', error);
        localStorage.removeItem('user');
        localStorage.removeItem('token');
      }
    }

    establishAnonymousSession().finally(() => setIsLoading(false));
  }, []);

  // Anonymous identity is a real bearer token now, and tokens expire (30 days,
  // backend/app/database.py). api.ts's response interceptor clears the stale
  // credentials and fires this event on any 401; without recovering here, a
  // returning visitor past expiry would be stuck in a chat that fails forever.
  // /api/auth/anonymous is called with fetch(), not apiClient, so a failure to
  // re-establish cannot re-trigger this handler — no loop.
  useEffect(() => {
    const handleUnauthorized = () => {
      setUser(null);
      setToken(null);
      establishAnonymousSession();
    };
    window.addEventListener('auth:unauthorized', handleUnauthorized);
    return () => window.removeEventListener('auth:unauthorized', handleUnauthorized);
  }, []);

  const login = async (email: string, password: string) => {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, tenant_id: currentTenantId() }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Login failed');
    }

    const data: AuthResponse = await response.json();
    const loggedInUser: User = { user_id: data.user_id, email: data.email, name: data.name, is_anonymous: false };

    setUser(loggedInUser);
    setToken(data.token);
    setAuthError(null);
    localStorage.setItem('user', JSON.stringify(loggedInUser));
    localStorage.setItem('token', data.token);
    setShowLoginScreen(false);
  };

  const register = async (email: string, name: string, password: string) => {
    const response = await fetch('/api/auth/register', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, name, password, tenant_id: currentTenantId() }),
    });

    if (!response.ok) {
      const error = await response.json();
      throw new Error(error.detail || 'Registration failed');
    }

    const data: AuthResponse = await response.json();
    const registeredUser: User = { user_id: data.user_id, email: data.email, name: data.name, is_anonymous: false };

    setUser(registeredUser);
    setToken(data.token);
    setAuthError(null);
    localStorage.setItem('user', JSON.stringify(registeredUser));
    localStorage.setItem('token', data.token);
    setShowLoginScreen(false);
  };

  const logout = async () => {
    localStorage.removeItem('currentSessionId');

    // Drop straight back into a fresh anonymous session rather than a
    // visible gate — logging out of a registered account should feel like
    // "back to guest," not "locked out." If re-establishing anonymous
    // session fails, authError is set (by establishAnonymousSession) so
    // MainApp shows a recoverable error state rather than a blank page.
    setIsLoading(true);
    setUser(null);
    setToken(null);
    localStorage.removeItem('user');
    localStorage.removeItem('token');
    await establishAnonymousSession();
    setIsLoading(false);
  };

  // Exposed so a visitor stuck on the post-failure error state (see
  // MainApp) can retry without a full page reload.
  const retryAnonymousSession = async () => {
    setIsLoading(true);
    await establishAnonymousSession();
    setIsLoading(false);
  };

  const value: AuthContextType = {
    user,
    token,
    isLoading,
    authError,
    showLoginScreen,
    openLoginScreen: () => setShowLoginScreen(true),
    closeLoginScreen: () => setShowLoginScreen(false),
    login,
    register,
    logout,
    retryAnonymousSession,
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (context === undefined) {
    throw new Error('useAuth must be used within an AuthProvider');
  }
  return context;
}
