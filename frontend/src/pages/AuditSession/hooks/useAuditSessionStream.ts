import { useEffect, useRef } from "react";

export function useAuditSessionStream(refresh: () => Promise<void> | void, enabled: boolean) {
  const refreshRef = useRef(refresh);

  useEffect(() => {
    refreshRef.current = refresh;
  }, [refresh]);

  useEffect(() => {
    if (!enabled) {
      return;
    }
    const timer = window.setInterval(() => {
      void refreshRef.current();
    }, 5000);
    return () => window.clearInterval(timer);
  }, [enabled]);
}