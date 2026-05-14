import { FC, useState } from "react";
import {
  FaRegThumbsUp,
  FaThumbsUp,
  FaRegThumbsDown,
  FaThumbsDown,
} from "react-icons/fa";
import { IoMdCopy } from "react-icons/io";
import { PiArrowsCounterClockwiseFill } from "react-icons/pi";
import { Feedback, Message } from "@/actions/ActionProvider";
import { PiGraph } from "react-icons/pi";
import { FaTable } from "react-icons/fa";
import { LuInfo, LuActivity } from "react-icons/lu";
import { useRoles } from "@/hooks/useRoles";
const GRAPHRAG_URL = "";

interface Interactions {
  message?: any;
  showExplain: () => boolean;
  showTable: () => boolean;
  showGraph: () => boolean;
  onViewTrace?: () => void;
}

export const Interactions: FC<Interactions> = ({
  message,
  showExplain,
  showTable,
  showGraph,
  onViewTrace,
}: Interactions) => {
  // Seed from the persisted feedback when re-rendering a history
  // message so the up/down state matches what the user already
  // submitted before the page reloaded.
  const [feedback, setFeedback] = useState<Feedback>(
    (message?.feedback as Feedback) ?? Feedback.NoFeedback
  );
  const { isSuperuser, isGlobalDesigner, isGraphAdmin } = useRoles();
  const canViewTrace = isSuperuser || isGlobalDesigner || isGraphAdmin;

  const sendFeedback = async (action: Feedback, message: Message) => {
    const creds = sessionStorage.getItem("creds");
    setFeedback(action);
    message.feedback = action;
    await fetch(`${GRAPHRAG_URL}/ui/feedback`, {
      method: "POST",
      body: JSON.stringify(message),
      headers: {
        Authorization: `Basic ${creds}`,
        "Content-Type": "application/json",
      },
    });
  };

  // Suppress the toolbar for non-answer message types where the
  // buttons would be meaningless (progress chips, greeting cards,
  // hard errors).
  const responseType = message?.response_type;
  if (responseType === "progress" || responseType === "greeting" || responseType === "error") {
    return null;
  }
  // Hide the row entirely for the welcome / loading placeholder
  // bubble that has neither a real answer nor an answered question.
  if (!message?.content && !message?.answered_question) {
    return null;
  }

  const hasGraphData = Boolean(message?.query_sources?.result?.edges);
  const hasTableData = Boolean(message?.query_sources?.result);
  // The trace page is keyed by message_id. Some history payloads pre-date
  // the message_id capture, so suppress the button when we can't build a
  // valid /trace/<id> URL — otherwise the click opens a blank tab.
  const traceMessageId = message?.messageId || message?.message_id || "";
  const hasTraceId = Boolean(traceMessageId);

  return (
    <div className="flex mt-3">
      {true ? (
        <>
          <div
            className="w-[28px] h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 cursor-pointer"
            onClick={() => {
              if (feedback !== Feedback.LIKE) {
                sendFeedback(Feedback.LIKE, message);
              } else {
                sendFeedback(Feedback.NoFeedback, message);
              }
            }}
          >
            {feedback === Feedback.LIKE ? <FaThumbsUp /> : <FaRegThumbsUp />}
          </div>

          <div
            className="w-[28px] h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 cursor-pointer"
            onClick={() => {
              if (feedback !== Feedback.DISLIKE) {
                sendFeedback(Feedback.DISLIKE, message);
              } else {
                sendFeedback(Feedback.NoFeedback, message);
              }
            }}
          >
            {feedback === Feedback.DISLIKE ? (
              <FaThumbsDown />
            ) : (
              <FaRegThumbsDown />
            )}
          </div>

          {/* <div
            className="w-[28px] h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 cursor-pointer"
            onClick={() => alert("Copy!!")}
          >
            <IoMdCopy className="text-[15px]" />
          </div> */}

          {/* <div
            className="w-[28px] h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 cursor-pointer"
            onClick={() => alert("Regenerate!!")}
          >
            <PiArrowsCounterClockwiseFill className="text-[15px]" />
          </div> */}

          {canViewTrace && hasTraceId ? (
            <div
              className="w-auto h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 px-2 cursor-pointer"
              onClick={() => onViewTrace?.()}
            >
              <LuActivity className="text-[15px] mr-1" />
              <span className="text-xs">View Trace</span>
            </div>
          ) : (
            <div
              className="w-auto h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 px-2 cursor-pointer"
              onClick={() => showExplain()}
            >
              <LuInfo className="text-[15px] mr-1" />
              <span className="text-xs">Explain</span>
            </div>
          )}

          <div
            className={`w-[28px] h-[28px] bg-shadeA flex items-center justify-center rounded-sm ml-5 mr-1 ${
              hasGraphData ? 'cursor-pointer' : 'cursor-not-allowed opacity-50'
            }`}
            title={hasGraphData ? "Show graph" : "No graph data for this answer"}
            onClick={() => {
              if (hasGraphData) {
                showGraph();
              }
            }}
          >
            <PiGraph className="text-[15px]" />
          </div>

          <div
            className={`w-[28px] h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 ${
              hasTableData ? 'cursor-pointer' : 'cursor-not-allowed opacity-50'
            }`}
            title={hasTableData ? "Show table" : "No table data for this answer"}
            onClick={() => {
              if (hasTableData) {
                showTable();
              }
            }}
          >
            <FaTable className="text-[15px]" />
          </div>

        </>
      ) : null}
    </div>
  );
}
