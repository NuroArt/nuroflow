import { Activity, GitBranch, LayoutDashboard, LogOut, Plug } from "lucide-react";
import type { ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useAuth } from "../lib/auth";

const NAV = [
  { to: "/", label: "Overview", Icon: LayoutDashboard, end: true },
  { to: "/workflows", label: "Workflows", Icon: GitBranch },
  { to: "/runs", label: "Runs", Icon: Activity },
  { to: "/integrations", label: "Integrations", Icon: Plug },
];

export function Logo() {
  return (
    <div className="flex items-center gap-2.5">
      <svg viewBox="0 0 32 32" className="h-7 w-7" aria-hidden>
        <rect width="32" height="32" rx="8" fill="#0b0f17" stroke="#1f2939" />
        <path d="M8 22V10l8 8 8-8v12" fill="none" stroke="#3d8bff" strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />
      </svg>
      <span className="text-[15px] font-semibold tracking-tight">
        Nuro<span className="text-accent">Flow</span>
      </span>
    </div>
  );
}

export function Layout() {
  const { state, logout } = useAuth();
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 flex h-screen w-56 shrink-0 flex-col border-r border-line bg-surface-1/60 px-3 py-4 max-md:hidden">
        <div className="px-2 pb-6"><Logo /></div>
        <nav className="flex flex-col gap-0.5">
          {NAV.map(({ to, label, Icon, end }) => (
            <NavLink
              key={to}
              to={to}
              end={end}
              className={({ isActive }) =>
                `flex items-center gap-2.5 rounded-lg px-2.5 py-2 text-sm transition-colors ${
                  isActive ? "bg-accent/10 text-ink shadow-glow" : "text-ink-muted hover:bg-surface-2 hover:text-ink"
                }`
              }
            >
              <Icon className="h-4 w-4" aria-hidden /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto border-t border-line px-2 pt-3">
          {state.status === "signedIn" && (
            <p className="truncate text-xs text-ink-muted" title={state.user.email}>{state.user.email}</p>
          )}
          <button className="mt-2 flex items-center gap-2 text-xs text-ink-faint hover:text-ink" onClick={() => void logout()}>
            <LogOut className="h-3.5 w-3.5" aria-hidden /> Sign out
          </button>
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <MobileNav />
        <div className="mx-auto max-w-7xl px-4 py-6 md:px-8">
          <Outlet />
        </div>
      </main>
    </div>
  );
}

function MobileNav() {
  return (
    <nav className="flex gap-1 overflow-x-auto border-b border-line px-4 py-2 md:hidden">
      {NAV.map(({ to, label, end }) => (
        <NavLink key={to} to={to} end={end}
          className={({ isActive }) => `whitespace-nowrap rounded-md px-2.5 py-1 text-sm ${isActive ? "bg-accent/15 text-ink" : "text-ink-muted"}`}>
          {label}
        </NavLink>
      ))}
    </nav>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: ReactNode; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <header className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div className="min-w-0">
        <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-ink-muted">{subtitle}</p>}
      </div>
      {actions && <div className="flex items-center gap-2">{actions}</div>}
    </header>
  );
}
