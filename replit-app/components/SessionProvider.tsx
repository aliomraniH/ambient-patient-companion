"use client";

import { useEffect, type ReactNode } from "react";

export default function SessionProvider({ children }: { children: ReactNode }) {
  useEffect(() => {
    fetch("/api/session", { method: "POST" }).catch(() => {});
  }, []);

  return <>{children}</>;
}
