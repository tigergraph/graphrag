import React, { useState } from "react";
import { Link } from "react-router-dom";
import { BookOpen, Check, ChevronDown, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

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

For the structural step, say which match to use: an exact case-insensitive match on issue_key when the full key is known, or issue_key ending with the number when only a number is known. Name the attributes and neighbour names to return. Returning the vertex alone is not enough.

A count, list, or filter across many issues stays a structural query. Add hybrid search only when that question also needs description, comment, or change-log text.

The final answer step depends on every retrieval step.`;

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

const GUIDES: Record<string, ConnectorGuide> = {
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
  const [openId, setOpenId] = useState<string | null>(null);
  const [copiedId, setCopiedId] = useState<string | null>(null);

  if (!guide) return null;

  const copyExample = async (example: ExamplePrompt) => {
    try {
      await navigator.clipboard.writeText(example.text);
      setCopiedId(example.id);
      window.setTimeout(() => {
        setCopiedId((current) => (current === example.id ? null : current));
      }, 2000);
    } catch {
      setCopiedId(null);
    }
  };

  return (
    <div className="mt-2 border-t border-gray-200 dark:border-[#3D3D3D] pt-5 space-y-4 min-w-0 max-w-full">
      <div className="flex items-start gap-3">
        <BookOpen className="h-4 w-4 mt-1 shrink-0 text-black dark:text-white" />
        <div className="min-w-0">
          <h3 className="font-medium text-black dark:text-white">
            4. Prompt guide
          </h3>
          <p className="text-sm text-gray-600 dark:text-gray-400 mt-1">
            How to write the {guide.title} prompt for this graph.
          </p>
        </div>
      </div>

      <div className="min-w-0 space-y-2">
        <h4 className="text-sm font-medium text-black dark:text-white">
          How to build the prompt
        </h4>
        <p className="text-sm text-gray-700 dark:text-gray-300">{guide.summary}</p>
        <ul className="text-sm text-gray-700 dark:text-gray-300 list-disc pl-5 space-y-1">
          {guide.points.map((point) => (
            <li key={point}>{point}</li>
          ))}
        </ul>
      </div>

      <div className="rounded-md border border-blue-200 bg-blue-50 p-4 text-sm text-blue-800 dark:border-blue-800 dark:bg-blue-900/20 dark:text-blue-200">
        These examples are an addition, not a replacement. Open{" "}
        <Link to="/setup/prompts" className="font-medium underline">
          Customize Prompts
        </Link>
        , select this graph, and open the prompt named on the example. Leave
        the instructions already in that prompt, scroll to the end, and paste
        the example there. Then save. Do not delete the existing prompt text.
      </div>

      <div className="min-w-0 space-y-3">
        <h4 className="text-sm font-medium text-black dark:text-white">
          Example prompts
        </h4>
        {guide.examples.map((example) => {
          const open = openId === example.id;
          return (
            <div
              key={example.id}
              className="min-w-0 max-w-full border border-gray-200 dark:border-[#3D3D3D] rounded-md"
            >
              <div className="flex flex-wrap items-center justify-between gap-3 p-3">
                <button
                  type="button"
                  className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  aria-expanded={open}
                  onClick={() => setOpenId(open ? null : example.id)}
                >
                  <ChevronDown
                    className={cn(
                      "h-4 w-4 shrink-0 transition-transform",
                      open && "rotate-180"
                    )}
                  />
                  <span className="min-w-0">
                    <span className="block text-sm font-medium text-black dark:text-white">
                      {example.name}
                    </span>
                    <span className="block text-xs text-gray-500 dark:text-gray-400 break-words">
                      Paste into {example.where}
                    </span>
                  </span>
                </button>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="shrink-0"
                  onClick={() => copyExample(example)}
                >
                  {copiedId === example.id ? (
                    <Check className="h-4 w-4 mr-2" />
                  ) : (
                    <Copy className="h-4 w-4 mr-2" />
                  )}
                  {copiedId === example.id ? "Copied" : "Copy"}
                </Button>
              </div>
              {open && (
                <pre className="mx-3 mb-3 max-h-80 max-w-full overflow-auto whitespace-pre-wrap break-words rounded-md border border-gray-200 bg-gray-50 p-3 text-xs leading-5 text-gray-800 dark:border-[#3D3D3D] dark:bg-background dark:text-gray-200">
                  {example.text}
                </pre>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
};

export default ConnectorPromptGuide;
