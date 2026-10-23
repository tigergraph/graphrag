export interface JiraSource {
  id: string;
  type: "jira_cloud";
  enabled: boolean;
  display_name: string;
  connection: {
    site_url: string;
    email: string;
    api_token: string;
    cloud_id?: string | null;
  };
  scope: {
    project_keys: string[];
    created_after?: string | null;
    updated_after?: string | null;
    status_categories?: Array<"new" | "indeterminate" | "done">;
    jql_extra: string;
    include_comments: boolean;
    story_points_field?: string | null;
  };
  sync: {
    overlap_seconds: number;
    checkpoint?: string | null;
    last_tested_at?: string | null;
    last_started_at?: string | null;
    last_completed_at?: string | null;
    last_error?: string | null;
    last_issue_count: number;
  };
}

export interface JiraSchemaStatus {
  status:
    | "not_initialized"
    | "not_installed"
    | "incomplete"
    | "conflict"
    | "installed";
  missing: Record<string, string[]>;
  conflicts: string[];
}
