import { createContext, useContext, useEffect, useState, type ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { api, login as apiLogin, logout as apiLogout, refresh, session } from "./api";
import type { User } from "./types";

type AuthState =
  | { status: "loading" }
  | { status: "signedOut" }
  | { status: "signedIn"; user: User };

interface AuthApi {
  state: AuthState;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthApi | null>(null);

async function loadUser(): Promise<User> {
  return (await api<{ user: User }>("/auth/me")).user;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });

  useEffect(() => {
    // Resume a session after a reload: trade the stored refresh token for a new pair.
    let cancelled = false;
    (async () => {
      const ok = session.hasRefreshToken() && (await refresh());
      if (cancelled) return;
      setState(ok ? { status: "signedIn", user: await loadUser() } : { status: "signedOut" });
    })().catch(() => !cancelled && setState({ status: "signedOut" }));
    const off = session.onChange((signedIn) => {
      if (!signedIn) setState({ status: "signedOut" });
    });
    return () => {
      cancelled = true;
      off();
    };
  }, []);

  const value: AuthApi = {
    state,
    async login(email, password) {
      await apiLogin(email, password);
      setState({ status: "signedIn", user: await loadUser() });
    },
    async logout() {
      await apiLogout();
    },
  };
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthApi {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth outside AuthProvider");
  return ctx;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  const location = useLocation();
  if (state.status === "loading") return <FullPageSpinner />;
  if (state.status === "signedOut") return <Navigate to="/login" replace state={{ from: location }} />;
  return <>{children}</>;
}

export function FullPageSpinner() {
  return (
    <div className="grid min-h-screen place-items-center">
      <div className="h-6 w-6 animate-spin rounded-full border-2 border-accent border-t-transparent" />
    </div>
  );
}
