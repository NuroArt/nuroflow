import { Link, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { Empty } from "./components/ui";
import { RequireAuth } from "./lib/auth";
import { IntegrationsPage } from "./pages/Integrations";
import { LoginPage } from "./pages/Login";
import { OverviewPage } from "./pages/Overview";
import { RunDetailPage, RunsPage } from "./pages/Runs";
import { WorkflowDetailPage, WorkflowsPage } from "./pages/Workflows";

export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route index element={<OverviewPage />} />
        <Route path="workflows" element={<WorkflowsPage />} />
        <Route path="workflows/:id" element={<WorkflowDetailPage />} />
        <Route path="runs" element={<RunsPage />} />
        <Route path="runs/:id" element={<RunDetailPage />} />
        <Route path="integrations" element={<IntegrationsPage />} />
        <Route path="*" element={<Empty title="Page not found"><Link to="/" className="text-accent-strong">Back to overview</Link></Empty>} />
      </Route>
    </Routes>
  );
}
