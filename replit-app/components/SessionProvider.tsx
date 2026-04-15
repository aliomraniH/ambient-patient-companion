"use client";

import { useEffect } from "react";

export default function SessionProvider({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    fetch("/api/auth/session", { method: "POST", credentials: "same-origin" })
      .catch(() => {});
  }, []);

  return <>{children}</>;
}
