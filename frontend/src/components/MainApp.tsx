import { useState, useEffect, useRef } from 'react';
import { useAuth } from '../contexts/AuthContext';
import AuthScreen from './AuthScreen';
import SessionSidebar from './SessionSidebar';
import ChatInterface from './ChatInterface';
import RefundApprovals from './RefundApprovals';

export default function MainApp() {
  const {
    user,
    isLoading,
    authError,
    showLoginScreen,
    openLoginScreen,
    closeLoginScreen,
    logout,
    retryAnonymousSession,
  } = useAuth();
  const [currentSessionId, setCurrentSessionId] = useState<string | null>(null);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const userMenuRef = useRef<HTMLDivElement>(null);

  // Close user menu when clicking outside
  useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (userMenuRef.current && !userMenuRef.current.contains(event.target as Node)) {
        setShowUserMenu(false);
      }
    };

    if (showUserMenu) {
      document.addEventListener('mousedown', handleClickOutside);
      return () => {
        document.removeEventListener('mousedown', handleClickOutside);
      };
    }
  }, [showUserMenu]);

  // Load current session from localStorage on mount
  useEffect(() => {
    if (user) {
      const savedSessionId = localStorage.getItem('currentSessionId');
      if (savedSessionId) {
        setCurrentSessionId(savedSessionId);
      }
    }
  }, [user]);

  // Save current session to localStorage
  useEffect(() => {
    console.log('[MainApp] currentSessionId changed to:', currentSessionId);
    if (currentSessionId) {
      localStorage.setItem('currentSessionId', currentSessionId);
    } else {
      localStorage.removeItem('currentSessionId');
    }
  }, [currentSessionId]);

  const handleSessionSelect = (sessionId: string) => {
    console.log('[MainApp] handleSessionSelect called with:', sessionId);
    console.log('[MainApp] Current session before update:', currentSessionId);
    setCurrentSessionId(sessionId);
  };

  const handleNewSession = () => {
    setCurrentSessionId(null);
  };

  const handleSessionCreated = (sessionId: string) => {
    setCurrentSessionId(sessionId);
  };

  const handleLogout = async () => {
    setCurrentSessionId(null);
    await logout();
  };

  if (isLoading) {
    return (
      <div className="app-loading">
        <div className="loading-spinner"></div>
        <p>Loading...</p>
      </div>
    );
  }

  // Checked ahead of the `!user` branch below so "Sign in" is reachable even
  // when the silent anonymous session failed to start (user is still null)
  // — the login/register form doesn't depend on an anonymous session ever
  // having existed.
  if (showLoginScreen) {
    return <AuthScreen onCancel={closeLoginScreen} />;
  }

  if (!user) {
    // The silent anonymous-session attempt (startup or post-logout) failed.
    // Never leave this as a blank page: offer a retry and a manual way to
    // sign in with an existing account.
    return (
      <div className="app-loading">
        <p>{authError || 'Something went wrong. Please try again.'}</p>
        <div className="error-actions">
          <button className="btn-retry" onClick={() => retryAnonymousSession()}>
            Try again
          </button>
          <button className="btn-reload" onClick={openLoginScreen}>
            Sign in
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="main-app">
      <SessionSidebar
        currentSessionId={currentSessionId}
        onSessionSelect={handleSessionSelect}
        onNewSession={handleNewSession}
      />

      <div className="chat-container">
        <div className="chat-header">
          <div className="chat-header-left">
            <h1>Customer Support Chat</h1>
          </div>
          <div className="chat-header-right">
            <div className="user-profile" ref={userMenuRef}>
              <button
                className="user-profile-btn"
                onClick={() => setShowUserMenu(!showUserMenu)}
              >
                <div className="user-avatar">
                  {user.is_anonymous ? '👤' : (user.name?.[0] || user.email?.[0] || 'U')}
                </div>
                <span className="user-name">
                  {user.is_anonymous ? 'Guest' : (user.name || user.email)}
                </span>
              </button>

              {showUserMenu && (
                <div className="user-menu">
                  <div className="user-menu-header">
                    {!user.is_anonymous && (
                      <>
                        <div className="user-menu-name">{user.name}</div>
                        <div className="user-menu-email">{user.email}</div>
                      </>
                    )}
                    {user.is_anonymous && (
                      <div className="user-menu-name">Guest User</div>
                    )}
                  </div>
                  <div className="user-menu-divider"></div>
                  {user.is_anonymous && (
                    <button
                      className="user-menu-item"
                      onClick={() => {
                        setShowUserMenu(false);
                        openLoginScreen();
                      }}
                    >
                      Sign in
                    </button>
                  )}
                  <button
                    className="user-menu-item"
                    onClick={handleLogout}
                  >
                    Logout
                  </button>
                </div>
              )}
            </div>
          </div>
        </div>

        <RefundApprovals />

        <div className="chat-content">
          <ChatInterface
            currentSessionId={currentSessionId}
            onSessionCreated={handleSessionCreated}
          />
        </div>
      </div>
    </div>
  );
}
