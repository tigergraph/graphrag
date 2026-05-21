import { useState, useEffect, useCallback } from "react";

export interface RolesState {
  userRoles: string[];
  graphRoles: Record<string, string[]>;
  rolesLoaded: boolean;
  hasCreds: boolean;
  selectedGraph: string;
  isSuperuser: boolean;
  isGlobalDesigner: boolean;
  isGraphAdmin: boolean;
  canAccessSetup: boolean;
}

function parseGraphRoles(raw: unknown): Record<string, string[]> {
  if (!raw || typeof raw !== "object") return {};
  return Object.fromEntries(
    Object.entries(raw as Record<string, unknown>).map(([graph, roles]) => [
      graph,
      Array.isArray(roles)
        ? roles.map((role: string) => role.toLowerCase())
        : [],
    ])
  );
}

export function useRoles(refreshKey?: unknown): RolesState {
  const [userRoles, setUserRoles] = useState<string[]>([]);
  const [graphRoles, setGraphRoles] = useState<Record<string, string[]>>({});
  const [rolesLoaded, setRolesLoaded] = useState(false);
  const [hasCreds, setHasCreds] = useState(false);
  const [selectedGraph, setSelectedGraph] = useState(
    sessionStorage.getItem("selectedGraph") || ""
  );

  const loadRoles = useCallback(async () => {
    const creds = sessionStorage.getItem("auth");
    if (!creds) {
      setUserRoles([]);
      setGraphRoles({});
      setHasCreds(false);
      setRolesLoaded(true);
      return;
    }

    // Try loading from sessionStorage first (populated at login)
    const site = JSON.parse(sessionStorage.getItem("site") || "{}");
    if (Array.isArray(site.roles)) {
      const roles = site.roles.map((role: string) => role.toLowerCase());
      setUserRoles(roles);
      setGraphRoles(parseGraphRoles(site.graph_roles));
      setSelectedGraph(sessionStorage.getItem("selectedGraph") || "");
      setHasCreds(true);
      setRolesLoaded(true);
      return;
    }

    // Fallback: fetch from backend (for sessions created before login returned roles)
    try {
      const response = await fetch("/ui/roles", {
        headers: { Authorization: creds! },
      });
      if (!response.ok) {
        setUserRoles([]);
        setGraphRoles({});
        setHasCreds(false);
        return;
      }
      const data = await response.json();
      const roles = Array.isArray(data.roles) ? data.roles : [];
      setUserRoles(roles.map((role: string) => role.toLowerCase()));
      setGraphRoles(parseGraphRoles(data.graph_roles));
      setSelectedGraph(sessionStorage.getItem("selectedGraph") || "");
      setHasCreds(true);

      // Persist to site so subsequent reads don't need a fetch
      site.roles = data.roles;
      site.graph_roles = data.graph_roles;
      sessionStorage.setItem("site", JSON.stringify(site));
    } catch (err) {
      console.error("Failed to fetch user roles:", err);
      setUserRoles([]);
      setGraphRoles({});
      setHasCreds(false);
    } finally {
      setRolesLoaded(true);
    }
  }, []);

  useEffect(() => {
    loadRoles();
  }, [loadRoles, refreshKey]);

  useEffect(() => {
    const handleGraphChange = () => {
      setSelectedGraph(sessionStorage.getItem("selectedGraph") || "");
    };
    window.addEventListener("graphrag:selectedGraph", handleGraphChange);
    return () => {
      window.removeEventListener("graphrag:selectedGraph", handleGraphChange);
    };
  }, []);

  const selectedGraphRoles = graphRoles[selectedGraph] || [];
  const isSuperuser = userRoles.includes("superuser");
  const isGlobalDesigner = userRoles.includes("globaldesigner");
  const isGraphAdmin = selectedGraphRoles.includes("admin");
  const isAdminOnAnyGraph = (Object.values(graphRoles) as string[][]).some(roles => roles.includes("admin"));
  const canAccessSetup = isSuperuser || isGlobalDesigner || isAdminOnAnyGraph;

  return {
    userRoles,
    graphRoles,
    rolesLoaded,
    hasCreds,
    selectedGraph,
    isSuperuser,
    isGlobalDesigner,
    isGraphAdmin,
    canAccessSetup,
  };
}
