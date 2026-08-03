import { createContext, useContext, useState, useEffect, ReactNode } from 'react';
import type { User, AuthResponse, AnonymousUserResponse } from '../types';
import { currentTenantId } from '../services/api';

interface AuthContextType {
  user: User | null;
  token: string | null;
  isLoading: boolean;
  showLoginScreen: boolean;
  openLoginScreen: () => void;
  closeLoginScreen: () => void;
  login: (email: string, password: string) => Promise<void>;
  register: (email: string, name: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextType | undefined>(undefined);

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
  const [showLoginScreen, setShowLoginScreen] = useState(false);

  // On load: use a stored session if there is one, otherwise silently start
  // an anonymous one. There is no visible gate — the visitor always lands
  // in a ready chat, whether that's a returning session or a brand-new
  // anonymous one.
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

    createAnonymousSession()
      .then(({ user: anonUser, token: anonToken }) => {
        setUser(anonUser);
        setToken(anonToken);
        localStorage.setItem('user', JSON.stringify(anonUser));
        localStorage.setItem('token', anonToken);
      })
      .catch((error) => {
        console.error('Failed to start anonymous session:', error);
      })
      .finally(() => setIsLoading(false));
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
    localStorage.setItem('user', JSON.stringify(registeredUser));
    localStorage.setItem('token', data.token);
    setShowLoginScreen(false);
  };

  const logout = async () => {
    localStorage.removeItem('currentSessionId');

    // Drop straight back into a fresh anonymous session rather than a
    // visible gate — logging out of a registered account should feel like
    // "back to guest," not "locked out."
    setIsLoading(true);
    try {
      const { user: anonUser, token: anonToken } = await createAnonymousSession();
      setUser(anonUser);
      setToken(anonToken);
      localStorage.setItem('user', JSON.stringify(anonUser));
      localStorage.setItem('token', anonToken);
    } catch (error) {
      console.error('Failed to start anonymous session after logout:', error);
      setUser(null);
      setToken(null);
      localStorage.removeItem('user');
      localStorage.removeItem('token');
    } finally {
      setIsLoading(false);
    }
  };

  const value: AuthContextType = {
    user,
    token,
    isLoading,
    showLoginScreen,
    openLoginScreen: () => setShowLoginScreen(true),
    closeLoginScreen: () => setShowLoginScreen(false),
    login,
    register,
    logout,
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
