import { useState, type FormEvent } from "react";
import { Navigate, useLocation, useNavigate } from "react-router-dom";
import { Logo } from "../components/Layout";
import { ApiError } from "../lib/api";
import { useAuth } from "../lib/auth";

export function LoginPage() {
  const { state, login } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const from = (location.state as { from?: { pathname: string } } | null)?.from?.pathname ?? "/";

  if (state.status === "signedIn") return <Navigate to={from} replace />;

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      navigate(from, { replace: true });
    } catch (err) {
      setError(err instanceof ApiError && err.status === 429 ? err.message : "Invalid email or password.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="grid min-h-screen place-items-center bg-[radial-gradient(ellipse_at_top,_#3d8bff1f,_transparent_60%)] px-4">
      <form onSubmit={submit} className="card w-full max-w-sm p-6 shadow-glow">
        <Logo />
        <h1 className="mt-6 text-lg font-semibold">Sign in</h1>
        <p className="mt-1 text-sm text-ink-muted">Workflow orchestration, observed live.</p>
        <label className="mt-6 block">
          <span className="label">Email</span>
          <input className="input mt-1.5" type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} />
        </label>
        <label className="mt-4 block">
          <span className="label">Password</span>
          <input className="input mt-1.5" type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
        </label>
        {error && <p className="mt-4 text-sm text-fail" role="alert">{error}</p>}
        <button className="btn-primary mt-6 w-full justify-center py-2" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}
