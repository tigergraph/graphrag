import { Moon, Sun, LogOut, Settings } from "lucide-react";
import { useState, useEffect } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { useTheme } from "@/components/ThemeProvider";
import { useConfirm } from "@/hooks/useConfirm";
import { useRoles } from "@/hooks/useRoles";

export function ModeToggle() {
  const { setTheme } = useTheme();
  const navigate = useNavigate();
  const location = useLocation();
  const isLoginRoute = location.pathname === "/";
  const [confirm, confirmDialog] = useConfirm();
  const { rolesLoaded, canAccessSetup } = useRoles(location.pathname);
  // Disable Settings / Logout while a chat answer is streaming so the
  // user can't accidentally unmount Chat and lose the in-flight reply.
  const [chatStreaming, setChatStreaming] = useState(false);
  useEffect(() => {
    const onStart = () => setChatStreaming(true);
    const onEnd = () => setChatStreaming(false);
    window.addEventListener("chat:streaming-start", onStart);
    window.addEventListener("chat:streaming-end", onEnd);
    return () => {
      window.removeEventListener("chat:streaming-start", onStart);
      window.removeEventListener("chat:streaming-end", onEnd);
    };
  }, []);
  const streamingTitle = chatStreaming
    ? "Disabled while the chat is generating an answer"
    : undefined;

  const handleLogout = async () => {
    // Show confirmation dialog; flag pending-chat loss when applicable
    // so the user isn't surprised by a generic error-answer afterwards.
    // Chat history itself is server-side and survives logout.
    const message = chatStreaming
      ? "An answer is still being generated. Logging out will drop the connection, and the in-flight answer will be lost (saved as an error in your history). Continue?"
      : "Log out of GraphRAG? You'll need to sign in again to continue.";
    const shouldLogout = await confirm(message);
    if (!shouldLogout) {
      return;
    }

    // Clear all localStorage data
    localStorage.clear();
    
    // Clear sessionStorage
    sessionStorage.clear();
    
    // Clear any cookies
    document.cookie.split(";").forEach(function(c) { 
      document.cookie = c.replace(/^ +/, "").replace(/=.*/, "=;expires=" + new Date().toUTCString() + ";path=/"); 
    });
    
    // Redirect to login page
    navigate("/");
  };

  const handleSetup = () => {
    navigate("/setup");
  };

  return (
    <div className="fixed right-4 top-[13px] z-[60] flex items-center gap-2">
      {!isLoginRoute && rolesLoaded && canAccessSetup && (
        <Button
          variant="outline"
          className="dark:border-[#3D3D3D]"
          onClick={handleSetup}
          disabled={chatStreaming}
          title={streamingTitle || "Setup"}
        >
          <Settings className="h-[1rem] w-[1rem]" />
        </Button>
      )}

      {!isLoginRoute && (
        <Button
          variant="outline"
          className="dark:border-[#3D3D3D]"
          onClick={handleLogout}
          title="Logout"
        >
          <LogOut className="h-[1rem] w-[1rem]" />
        </Button>
      )}
      
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="outline" className="dark:border-[#3D3D3D]">
            <Sun className="h-[1rem] w-[1rem] rotate-0 scale-100 transition-all dark:-rotate-90 dark:scale-0" />
            <Moon className="absolute h-[1rem] w-[1rem] rotate-90 scale-0 transition-all dark:rotate-0 dark:scale-100" />
            <span className="sr-only">Toggle theme</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem onClick={() => setTheme("light")}>
            Light
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => setTheme("dark")}>
            Dark
          </DropdownMenuItem>
          <DropdownMenuItem onClick={() => setTheme("system")}>
            System
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>

      {/* User Confirmation Dialog */}
      {confirmDialog}
    </div>
  );
}
