import { NavLink, Route, Routes } from "react-router-dom";

import BugDetail from "./pages/BugDetail";
import Queue from "./pages/Queue";
import Settings from "./pages/Settings";

function NavTab({ to, children }: { to: string; children: React.ReactNode }) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        `px-3 py-2 text-sm rounded-md transition-colors ${
          isActive
            ? "bg-secondary text-secondary-foreground"
            : "text-muted-foreground hover:text-foreground"
        }`
      }
      end
    >
      {children}
    </NavLink>
  );
}

export default function App() {
  return (
    <div className="min-h-screen bg-background text-foreground">
      <header className="border-b">
        <div className="container flex items-center justify-between py-3">
          <div className="font-semibold tracking-tight">RA Triage</div>
          <nav className="flex items-center gap-1">
            <NavTab to="/">Queue</NavTab>
            <NavTab to="/settings">Settings</NavTab>
          </nav>
        </div>
      </header>
      <main className="container py-6">
        <Routes>
          <Route path="/" element={<Queue />} />
          <Route path="/bugs/:id" element={<BugDetail />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
