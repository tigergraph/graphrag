import React from "react";
import { Link } from "react-router-dom";
import { BookOpen, CheckCircle2, GitBranch, SlidersHorizontal } from "lucide-react";

interface ExamplePrompt {
  id: string;
  name: string;
  where: string;
  text: string;
}

interface ConnectorGuide {
  title: string;
  summary: string;
  points: string[];
  examples: ExamplePrompt[];
}

const JIRA_PLANNER_EXAMPLE = `## Jira issues
Each Jira issue is stored in two places. They answer different parts of the same question, so a question about one issue needs both.

- Structural (graphrag__structural_retrieve): the current record. JiraIssue attributes are issue_key, summary, issue_type, status, status_category, priority, resolution, labels, components, fix_versions, created, updated, due, url, story_points. Assignee and reporter are JiraUser vertices reached by JIRA_ASSIGNED_TO and JIRA_REPORTED_BY. Project, parent, and issue links are edges. Comment text is JiraComment.body. Status, assignee, priority, and resolution changes are JiraChange events (field, from_value, to_value, created) reached by JIRA_HAS_CHANGE. The person who made a change is JIRA_CHANGE_BY.
- Unstructured (graphrag__hybrid_search): short document chunks for that issue. Each chunk starts with the issue key. They hold the description, comments, and one chunk per change: who changed status, assignee, priority, or resolution, when, and from which value to which.

Decide which issue the user means, using this message and ## Conversation:
- A project key plus a number (any casing, such as gml-2191) identifies one issue.
- Only a number (ticket 2191, summary of 2191) does not. Retrieve every issue_key that ends with that number so the answer can ask which project they mean. Do not plan a full write-up of one issue until a single key is known.
- A follow-up that does not repeat the key refers to the issue already discussed. Carry that key into every retrieval step.

Once one issue is identified, plan both retrievals. Put the full issue key and the aspect the user asked about into both tool questions. Set hybrid top_k to at least 8.
- A broad question (what is it, summarize, tell me about) needs the current fields plus the description, recent comments, and notable changes.
- History, timeline, progress, or who did what needs the JiraChange events and the comment text, together with the current status, assignee, and resolution.
- Release, fix version, due date, or when it will be fixed needs fix_versions, due, status, and resolution, plus any release or fix mentioned in the description or comments.

For the structural step, say which match to use: an exact case-insensitive match on issue_key when the full key is known, or issue_key ending with the number when only a number is known. Name the attributes and neighbour names to return. Returning the vertex alone is not enough. For a broad issue summary, request current fields, assignee, reporter, and project only. Request parent or linked issues only when the user asks about them, using a separate structural step for each relationship.

A count, list, or filter across many issues stays a structural query. Add hybrid search only when that question also needs description, comment, or change-log text.

The final answer step depends on every retrieval step.`;

const JIRA_QUERY_GUIDANCE_EXAMPLE = `## Jira issue queries
- For a broad single-issue lookup, retrieve the JiraIssue fields, assignee, reporter, and project. Do not add parent, issue-link, or comment relationships unless the question asks for them.
- Handle JIRA_HAS_PARENT and JIRA_LINKS_TO in a separate, focused graph query. If the relationship is requested, prefer MATCH over OPTIONAL MATCH.
- Return the related issue_key and requested edge attributes such as link_type. Do not return the parent or link edge object itself.`;

const JIRA_AGENT_EXAMPLE = `## Jira issues
Each issue has a current record in the graph and short document chunks. For one issue, call both graphrag__structural_retrieve and graphrag__hybrid_search before you answer. Use the conversation to resolve which issue is meant when this message does not repeat the key.

- Structural returns current fields: issue_key, summary, type, status, priority, resolution, labels, components, fix_versions, created, updated, due, url, story_points, plus assignee and reporter names. Comment text is JiraComment.body. Status, assignee, priority, and resolution changes are JiraChange events. Ask for an exact case-insensitive issue_key match when the full key is known. When the user gave only a number, ask for every issue_key that ends with that number and return the keys and summaries. Always ask for the attributes by name. A bare vertex id has no fields.
- Hybrid search (top_k at least 8) returns chunks that start with the issue key: the description, comments, and the change log. Include the issue key and the aspect the user asked about in the search text.

If several project keys share that number, ask which one they mean and list the full keys. Do not mix those issues into one answer.

Answer only what was asked, using both sources. A broad question gets the current fields plus the description and recent activity. A history question gets the change log in time order and the comments, with the current status beside it. A release or "when will this be fixed" question uses fix_versions, due, status, and resolution, plus any release mentioned in the text. If that fact is in neither source, say it is not available.`;

const JIRA_RESPONSE_EXAMPLE = `## Jira issues
Treat structured rows and document passages as one issue, not as competing answers.

- Structured rows are the current record: issue key, summary, status, priority, resolution, fix version, due date, url, assignee, reporter, labels, components, and links. Comment text is the comment body. Each status, assignee, priority, or resolution change is its own event: field, previous value, new value, who, and when.
- Document passages are the description, comments, and the change log. Use a passage only when it names the same issue key.

If the user named only a number and the context has more than one project key for it, ask which issue they mean and list the full keys. Do not include details from those issues. If only one key matches, confirm that key and then answer.

If the user message is only a full issue key, they are selecting that issue. Answer with its current fields and a short account of the description and recent activity.

Otherwise answer the question they asked.
- A broad question (what is it, summarize, tell me about) gets the title and url, the current fields, then the description and the recent comments or changes.
- History, timeline, progress, or who did what gets the change log in time order, then the comments. State the current status from the structured row.
- Release, fix version, or when it will be fixed uses fix version, due date, status, and resolution, plus any release or fix stated in the description or comments. If none of those say when or in which release, say that is not in the available data. An empty fix version is not a release. A due date of 1970-01-01 means no due date was set.

Prefer a structured value when the same field also appears in a passage. Do not invent a person, date, status, or release.`;

const ZENDESK_PLANNER_EXAMPLE = `## Zendesk tickets
A Zendesk ticket is a ZendeskTicket vertex. Its id is zendesk:<subdomain>:ticket:<id>, which is never a Jira issue key.

- Structural (graphrag__structural_retrieve): ticket_id, subject, status, and url. The requester is a ZendeskUser reached by ZENDESK_REQUESTED_BY. Comment text is ZendeskComment.body reached by ZENDESK_COMMENT_ON.
- Unstructured (graphrag__hybrid_search): chunks for the subject, description, and comments. A chunk starts with the ticket id.

When the question names a Jira issue and a Zendesk ticket, retrieve both and follow CROSS_SOURCE_LINK between JiraIssue and ZendeskTicket. Do not treat a ticket number as a Jira issue key.

If the question says ticket, issue, status, or assignee and does not say whether it means Jira or Zendesk, ask which source the user means.`;

const GUIDES: Record<string, ConnectorGuide> = {
  zendesk: {
    title: "Zendesk",
    summary:
      "Add a short Zendesk section to this graph's prompts. The connector loads that section only when the question is about Zendesk, and loads both sections when the question names Jira and Zendesk.",
    points: [
      "A Zendesk ticket id is not a Jira issue key. Keep the two records separate.",
      "Structural search returns the ticket, requester, and comment records. Hybrid search returns the description and comment text.",
      "A question that names both a Jira issue and a Zendesk ticket should follow CROSS_SOURCE_LINK.",
      "If the question could mean either source, ask the user which one they mean.",
    ],
    examples: [
      {
        id: "zendesk_planner",
        name: "Agentic Planner",
        where: "Customize Prompts → Agentic Planner, on this graph",
        text: ZENDESK_PLANNER_EXAMPLE,
      },
    ],
  },
  jira_cloud: {
    title: "Jira Cloud",
    summary:
      "Keep the prompt that is already on this graph. Add a short Jira section at the end so one ticket is answered from the current record and from the description, comments, and change history.",
    points: [
      "A project key plus a number, in any casing, is one ticket. Copy that key into both the structural question and the hybrid question.",
      "A number alone is not one ticket. Retrieve every issue key that ends with that number. If more than one project matches, ask which key they mean and list the full keys.",
      "A follow-up that does not repeat the key is about the ticket already in the conversation. Carry that key into the next retrieval.",
      "Structural search returns the current fields, the people, and the JiraChange events. Hybrid search returns the description, comment text, and one chunk per change. Use both for a question about one ticket.",
      "Do not put a real ticket key in the prompt. The model will treat that key as the ticket to retrieve. If fix version or due date is empty, say it is not available.",
    ],
    examples: [
      {
        id: "query_guidance",
        name: "Query Guidance",
        where: "Customize Prompts → Query Guidance, on this graph",
        text: JIRA_QUERY_GUIDANCE_EXAMPLE,
      },
      {
        id: "agentic_planner",
        name: "Agentic Planner",
        where: "Customize Prompts → Agentic Planner, on this graph",
        text: JIRA_PLANNER_EXAMPLE,
      },
      {
        id: "agentic_agent",
        name: "React Agent",
        where: "Customize Prompts → React Agent, on this graph",
        text: JIRA_AGENT_EXAMPLE,
      },
      {
        id: "chatbot_response",
        name: "Chatbot Responses",
        where: "Customize Prompts → Chatbot Responses, on this graph",
        text: JIRA_RESPONSE_EXAMPLE,
      },
    ],
  },
};

const ConnectorPromptGuide: React.FC<{ connectorType: string }> = ({
  connectorType,
}) => {
  const guide = GUIDES[connectorType];
  if (!guide) return null;

  const isJira = connectorType === "jira_cloud";
  const detection = isJira
    ? 'Selected when the question contains “Jira” or a Jira key such as GML-2186.'
    : 'Selected when the question contains “Zendesk” or a zendesk.com ticket URL.';
  const schemaScope = isJira
    ? "Planner and query generation receive only Jira connector guidance and Jira graph types."
    : "Planner and query generation receive only Zendesk connector guidance and Zendesk graph types.";

  return (
    <div className="mt-2 border-t border-gray-200 dark:border-[#3D3D3D] pt-5 space-y-4 min-w-0 max-w-full">
      <div className="flex items-start gap-3">
        <BookOpen className="h-4 w-4 mt-1 shrink-0 text-black dark:text-white" />
        <div className="min-w-0">
          <h3 className="font-medium text-black dark:text-white">
            4. Automatic prompt guidance
          </h3>
          <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
            {guide.title} guidance is selected by the backend for each question.
            No connector prompt needs to be copied into this graph.
          </p>
        </div>
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        <div className="rounded-md border border-gray-200 dark:border-[#3D3D3D] p-3">
          <CheckCircle2 className="h-4 w-4 text-green-600 dark:text-green-400 mb-2" />
          <h4 className="text-sm font-medium text-black dark:text-white">
            Source detection
          </h4>
          <p className="mt-1 text-xs leading-5 text-gray-600 dark:text-gray-400">
            {detection}
          </p>
        </div>
        <div className="rounded-md border border-gray-200 dark:border-[#3D3D3D] p-3">
          <SlidersHorizontal className="h-4 w-4 text-blue-600 dark:text-blue-400 mb-2" />
          <h4 className="text-sm font-medium text-black dark:text-white">
            Step-specific guidance
          </h4>
          <p className="mt-1 text-xs leading-5 text-gray-600 dark:text-gray-400">
            {schemaScope} The answer step receives the matching response rules.
          </p>
        </div>
        <div className="rounded-md border border-gray-200 dark:border-[#3D3D3D] p-3">
          <GitBranch className="h-4 w-4 text-tigerOrange mb-2" />
          <h4 className="text-sm font-medium text-black dark:text-white">
            Multiple sources
          </h4>
          <p className="mt-1 text-xs leading-5 text-gray-600 dark:text-gray-400">
            A question naming Jira and Zendesk loads both sets of guidance.
            Relationship questions also receive CROSS_SOURCE_LINK rules.
          </p>
        </div>
      </div>

      <div className="rounded-md border border-blue-200 bg-blue-50 p-4 text-sm text-blue-800 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-200 space-y-2">
        <div className="font-medium">What graph owners should configure</div>
        <p>
          Use{" "}
        <Link to="/setup/prompts" className="font-medium underline">
          Customize Prompts
        </Link>
          {" "}only for graph-wide business rules, terminology, tone, or output
          format that should apply regardless of the source. Do not paste Jira
          or Zendesk schema instructions there; doing so bypasses selective
          loading and sends those instructions with unrelated questions.
        </p>
      </div>

      <div className="rounded-md border border-gray-200 dark:border-[#3D3D3D] p-4 text-sm">
        <h4 className="font-medium text-black dark:text-white">
          Guidance ownership
        </h4>
        <p className="mt-1 text-gray-600 dark:text-gray-400">
          Connector guidance is product-managed and currently lives in
          <code className="mx-1 rounded bg-gray-100 dark:bg-background px-1 py-0.5 text-xs">
            connectors/prompt_selection.py
          </code>
          as separate planner, query, and answer rules. There is currently no
          user-editable connector-guidance field. If one is added later, it
          must keep those three steps separate and remain scoped to its
          connector instead of being appended to the global prompt.
        </p>
      </div>
    </div>
  );
};

export default ConnectorPromptGuide;
