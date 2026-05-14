import { useEffect, useRef, useCallback } from "react";

const DEFAULT_TIMEOUT_MS = 60 * 60 * 1000; // 1 hour

/**
 * Monitors user activity and clears the session after a period of inactivity.
 * Resets the timer on mouse, keyboard, scroll, and touch events.
 *
 * Components with long-running operations can pause/resume the timer:
 *   pauseIdleTimer()  — stops the countdown (e.g. before a long ingest call)
 *   resumeIdleTimer() — restarts the countdown (e.g. when the call finishes)
 */
export function useIdleTimeout(timeoutMs: number = DEFAULT_TIMEOUT_MS) {
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const handleTimeout = useCallback(() => {
    const creds = sessionStorage.getItem("creds");
    if (!creds) return; // Not logged in, nothing to do

    sessionStorage.clear();
    alert("Session expired due to inactivity. Please log in again.");
    window.location.href = "/";
  }, []);

  const resetTimer = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
    }
    // Only set timer if user is logged in
    if (sessionStorage.getItem("creds")) {
      timerRef.current = setTimeout(handleTimeout, timeoutMs);
    }
  }, [handleTimeout, timeoutMs]);

  const pause = useCallback(() => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  useEffect(() => {
    const events = ["mousemove", "mousedown", "keydown", "scroll", "touchstart"];

    const onPause = () => pause();
    const onResume = () => resetTimer();
    const onPing = () => resetTimer();

    events.forEach((event) => window.addEventListener(event, resetTimer));
    window.addEventListener("idle-timer-pause", onPause);
    window.addEventListener("idle-timer-resume", onResume);
    window.addEventListener("idle-timer-ping", onPing);
    resetTimer(); // Start the timer

    return () => {
      events.forEach((event) => window.removeEventListener(event, resetTimer));
      window.removeEventListener("idle-timer-pause", onPause);
      window.removeEventListener("idle-timer-resume", onResume);
      window.removeEventListener("idle-timer-ping", onPing);
      if (timerRef.current) {
        clearTimeout(timerRef.current);
      }
    };
  }, [resetTimer, pause]);
}

/** Pause the idle timer (e.g. during long-running backend operations). */
export function pauseIdleTimer() {
  window.dispatchEvent(new Event("idle-timer-pause"));
}

/** Resume the idle timer (e.g. when a long-running operation completes). */
export function resumeIdleTimer() {
  window.dispatchEvent(new Event("idle-timer-resume"));
}

/**
 * Reset the idle timer without requiring a user UI event. Call this
 * after a successful status poll for a long-running, user-initiated
 * backend flow (init, ingest, rebuild) so the session stays alive
 * while the user is watching progress.
 */
export function pingIdleTimer() {
  window.dispatchEvent(new Event("idle-timer-ping"));
}
