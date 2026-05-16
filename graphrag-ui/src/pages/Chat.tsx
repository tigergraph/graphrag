import React, { useEffect, useState } from "react";
import Bot from "@/components/Bot";
import SideMenu from "@/components/SideMenu";
import { ChevronLeft, ChevronRight } from "lucide-react";

// Sidebar width is user-resizable via the vertical separator. The
// chosen width is persisted to localStorage so it survives reloads.
const SIDEBAR_STORAGE_KEY = "graphrag:sidebarWidth";
const DEFAULT_SIDEBAR_WIDTH = 320;
const MIN_SIDEBAR_WIDTH = 220;
const MAX_SIDEBAR_WIDTH = 600;

const readStoredWidth = (): number => {
  try {
    const raw = parseInt(localStorage.getItem(SIDEBAR_STORAGE_KEY) || "");
    if (!isNaN(raw) && raw >= MIN_SIDEBAR_WIDTH && raw <= MAX_SIDEBAR_WIDTH) {
      return raw;
    }
  } catch {
    // localStorage may be unavailable (private mode); fall through.
  }
  return DEFAULT_SIDEBAR_WIDTH;
};

const Chat = () => {
  const [showSidebar, setShowSidebar] = useState<boolean>(true);
  const [sidebarWidth, setSidebarWidth] = useState<number>(readStoredWidth);
  const [isDragging, setIsDragging] = useState<boolean>(false);
  const [getConversationId, setGetConversationId] = useState<any>(['lkjh']);

  // Drag-to-resize. Track mouse globally while the user holds the
  // separator so the resize keeps working even if the cursor strays
  // outside the thin handle strip.
  useEffect(() => {
    if (!isDragging) return;
    const onMouseMove = (e: MouseEvent) => {
      const clamped = Math.max(
        MIN_SIDEBAR_WIDTH,
        Math.min(MAX_SIDEBAR_WIDTH, e.clientX)
      );
      setSidebarWidth(clamped);
    };
    const onMouseUp = () => {
      setIsDragging(false);
      try {
        localStorage.setItem(SIDEBAR_STORAGE_KEY, String(sidebarWidth));
      } catch {
        // ignore — width simply won't persist
      }
    };
    document.addEventListener("mousemove", onMouseMove);
    document.addEventListener("mouseup", onMouseUp);
    // Prevent text selection during drag.
    document.body.style.userSelect = "none";
    document.body.style.cursor = "col-resize";
    return () => {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.body.style.userSelect = "";
      document.body.style.cursor = "";
    };
  }, [isDragging, sidebarWidth]);

  return (
    <>
      {/* No `relative` on this container — adding one would create a
          stacking context that hides the top-right ModeToggle (Setup /
          Logout / theme) underneath the Bot header.  The chevron below
          positions itself against the viewport instead, which is fine
          because this container starts flush at the viewport edge. */}
      <div className="flex justify-between boxA bounce-3">
        {showSidebar ? (
          <SideMenu setGetConversationId={setGetConversationId} width={sidebarWidth} />
        ) : null}
        {/* Drag handle: thin vertical strip on the sidebar's right edge.
            Hovering shows a subtle highlight; mousedown enters resize
            mode. Sits behind the chevron so the chevron click still
            registers as toggle. */}
        {showSidebar && (
          <div
            onMouseDown={(e) => {
              e.preventDefault();
              setIsDragging(true);
            }}
            aria-label="Resize left menu"
            role="separator"
            className={
              "hidden md:block fixed top-0 bottom-0 z-10 w-1.5 cursor-col-resize " +
              "hover:bg-blue-500/30 dark:hover:bg-blue-400/30 transition-colors"
            }
            style={{ left: `${sidebarWidth - 3}px` }}
          />
        )}
        <button
          type="button"
          aria-label={showSidebar ? "Collapse left menu" : "Expand left menu"}
          onClick={() => setShowSidebar((prev) => !prev)}
          className={
            "hidden md:flex fixed top-1/2 -translate-y-1/2 z-20 " +
            "w-5 h-14 items-center justify-center cursor-pointer " +
            "bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] " +
            "rounded-r-md shadow-sm hover:bg-gray-100 dark:hover:bg-gray-800 " +
            "text-gray-600 dark:text-gray-300 transition-colors"
          }
          style={{ left: showSidebar ? `${sidebarWidth - 1}px` : "0" }}
        >
          {showSidebar ? (
            <ChevronLeft className="w-4 h-4" />
          ) : (
            <ChevronRight className="w-4 h-4" />
          )}
        </button>
        <Bot layout="fp" getConversationId={getConversationId} />
      </div>
    </>
  );
};

export default Chat;
