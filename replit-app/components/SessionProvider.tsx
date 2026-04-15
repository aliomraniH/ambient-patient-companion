"use client";

import { createContext, useContext, useEffect, useState, useCallback, type ReactNode } from "react";

interface AuthContextType {
  token: string | null;
  loading: boolean;
  authFetch: (url: string, init?: RequestInit) => Promise<Response>;
}

const AuthContext = createContext<AuthContextType>({
  token: null,
  loading: true,
  authFetch: () => Promise.reject(new Error("AuthContext not ready")),
});

export function useAuth() {
  return useContext(AuthContext);
}

export default function SessionProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const stored = sessionStorage.getItem("apc_token");
    if (stored) {
      setToken(stored);
      setLoading(false);
      return;
    }

    fetch("/api/auth/token", { method: "POST" })
      .then((res) => res.json())
      .then((data) => {
        if (data.access_token) {
          sessionStorage.setItem("apc_token", data.access_token);
          setToken(data.access_token);
        }
      })
      .catch((err) => {
        console.error("Auto-auth failed:", err);
      })
      .finally(() => setLoading(false));
  }, []);

  const authFetch = useCallback(
    (url: string, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      if (token) {
        headers.set("Authorization", `Bearer ${token}`);
      }
      return fetch(url, { ...init, headers });
    },
    [token]
  );

  return (
    <AuthContext.Provider value={{ token, loading, authFetch }}>
      {children}
    </AuthContext.Provider>
  );
}
