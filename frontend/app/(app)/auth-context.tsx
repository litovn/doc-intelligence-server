"use client";

import { createContext, useContext } from "react";
import type { AuthUser } from "@/lib/api";

// Populated by app/(app)/layout.tsx once GET /api/auth/me has resolved, so
// every page under this route group can read the logged-in user without a
// second network round-trip.
export const AuthContext = createContext<AuthUser | null>(null);

export function useAuth(): AuthUser {
  const user = useContext(AuthContext);
  if (!user) {
    throw new Error("useAuth() called outside the authenticated app layout");
  }
  return user;
}
