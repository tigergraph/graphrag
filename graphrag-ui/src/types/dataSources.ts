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
  token_available?: boolean;
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

export type ZendeskTicketStatus =
  | "new"
  | "open"
  | "pending"
  | "hold"
  | "solved"
  | "closed";

export interface ZendeskSource {
  id: string;
  type: "zendesk";
  enabled: boolean;
  display_name: string;
  token_available?: boolean;
  sync_running?: boolean;
  connection: {
    subdomain: string;
    email: string;
    api_token: string;
  };
  scope: {
    updated_after?: string | null;
    statuses?: ZendeskTicketStatus[];
    include_comments?: boolean;
    jira_integration_external_id?: string | null;
  };
  sync: {
    overlap_seconds: number;
    checkpoint?: string | null;
    last_ticket_id?: number | null;
    failed_ticket_ids?: string[];
    pending_comment_ticket_ids?: string[];
    last_tested_at?: string | null;
    last_started_at?: string | null;
    last_completed_at?: string | null;
    last_error?: string | null;
    last_link_error?: string | null;
    last_ticket_count: number;
    progress_checked?: number;
    progress_written?: number;
    progress_phase?: string | null;
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
  install?: {
    status: "queued" | "running" | "completed" | "failed";
    error?: string | null;
  } | null;
}
