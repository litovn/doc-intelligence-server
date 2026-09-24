"use client";

import { useEffect, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import Link from "next/link";
import { ApiError, logout, me, type AuthUser } from "@/lib/api";
import { AuthContext } from "./auth-context";

const NAV = [
  ["/", "Documents"],
  ["/tags", "Tags"],
  ["/chat", "Chat"],
] as const;

// Wraps "/", "/tags" and "/chat" (everything in this route group) — "/login"
// lives outside it. This is the only place session state is checked; pages
// below read it back via useAuth() instead of calling /api/auth/me again.
export default function AppLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  // The static export uses trailing slashes ("/tags/"), dev doesn't ("/tags").
  const pathname = usePathname().replace(/(.)\/$/, "$1");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [checking, setChecking] = useState(true);
  const [connectionError, setConnectionError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    me()
      .then((u) => {
        if (!cancelled) setUser(u);
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          router.replace("/login");
        } else {
          setConnectionError("Could not reach the server. Is the backend running?");
        }
      })
      .finally(() => {
        if (!cancelled) setChecking(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  async function handleLogout() {
    try {
      await logout();
    } finally {
      router.replace("/login");
    }
  }

  if (checking) return <p className="page-loading">Loading…</p>;
  if (connectionError) return <p className="page-error">{connectionError}</p>;
  if (!user) return null; // redirect to /login is in flight

  return (
    <AuthContext.Provider value={user}>
      <header className="app-header">
        <nav>
          {NAV.map(([href, label]) => (
            <Link key={href} href={href} aria-current={pathname === href ? "page" : undefined}>
              {label}
            </Link>
          ))}
        </nav>
        <div className="app-header-user">
          logged in as {user.username} ({user.level}) ·{" "}
          <button type="button" onClick={handleLogout}>
            log out
          </button>
        </div>
      </header>
      <main>{children}</main>
    </AuthContext.Provider>
  );
}
