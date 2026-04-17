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
  const [feedback, setFeedback] = useState(Feedback.NoFeedback);
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

  return (
    <div className="flex mt-3">
      {(message.query_sources?.result || message.query_sources?.cypher || message.query_sources?.answer) ? (
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

          {canViewTrace ? (
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
              message.query_sources?.result?.edges ? 'cursor-pointer' : 'cursor-not-allowed opacity-50'
            }`}
            onClick={() => {
              if (message.query_sources?.result?.edges) {
                showGraph();
              }
            }}
          >
            <PiGraph className="text-[15px]" />
          </div>

          <div
            className={`w-[28px] h-[28px] bg-shadeA flex items-center justify-center rounded-sm mr-1 ${
              message.query_sources?.result ? 'cursor-pointer' : 'cursor-not-allowed opacity-50'
            }`}
            onClick={() => {
              if (message.query_sources?.result) {
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
