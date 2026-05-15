import { FC, useState, useEffect } from "react";
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTrigger,
} from "@/components/ui/dialog"
import { ImEnlarge2 } from "react-icons/im";
import { IoIosCloseCircleOutline } from "react-icons/io";
import { Interactions } from "./Interact";
import { KnowledgeGraphPro } from "./graphs/KnowledgeGraphPro";
import { KnowledgeTablPro } from "./tables/KnowledgeTablePro";
import { useAlert } from "@/hooks/useAlert";
import TraceLogs from "@/pages/TraceLogs";
interface IChatbotMessageProps {
  message?: any;
  withAvatar?: boolean;
  loading?: boolean;
  messages: any[];
  delay?: number;
  id: number;
  setState?: React.Dispatch<any>;
  customComponents?: any;
  customStyles: {
    backgroundColor: string;
  };
}

const urlRegex = /https?:\/\//

// Phase 1.5 — render a subtle chip showing which retrieval method ran.
// Reads the auto-selection metadata that supportai_search mirrors into
// query_sources (chosen_retriever / chosen_retriever_reason / chosen_retriever_source).
const METHOD_LABELS: Record<string, string> = {
  similaritysearch: "Similarity",
  contextualsearch: "Contextual",
  hybridsearch: "Hybrid",
  communitysearch: "Community",
};

const RetrieverBadge: FC<{ message: any }> = ({ message }) => {
  const qs = message?.query_sources;
  if (!qs || typeof qs !== "object") return null;
  const method = qs.chosen_retriever as string | undefined;
  if (!method) return null;
  // Suppress for greetings / errors / progress events — those don't run a retriever.
  if (
    message.response_type === "progress" ||
    message.response_type === "greeting" ||
    message.response_type === "error"
  ) {
    return null;
  }
  const label = METHOD_LABELS[method] || method;
  const reason = (qs.chosen_retriever_reason as string | undefined) || "";
  const source = (qs.chosen_retriever_source as string | undefined) || "";
  const sourceLabel = source === "manual" ? "manual" : "auto";
  // Reason + source live in the hover tooltip so the inline chip stays
  // glanceable; users who want the detail can hover.
  const tooltip = reason ? `${sourceLabel}: ${reason}` : sourceLabel;
  return (
    <div
      className="inline-flex items-center gap-1.5 text-[11px] text-gray-500 dark:text-gray-400 bg-gray-100 dark:bg-shadeA rounded-full px-2 py-0.5 mt-1"
      title={tooltip}
    >
      <span>🔎</span>
      <span className="font-medium">{label}</span>
    </div>
  );
};

const getReasoning = (msg) => {
  
  if(msg.query_sources.reasoning instanceof Array) {
    const sources:Array<JSX.Element> = []
    for(let i = 0; i < msg.query_sources.reasoning.length; i++){
      const src = msg.query_sources.reasoning[i]
      if(urlRegex.test(src)){
        const a = (<li key={src}><a href={src} target='_blank' className='underline overflow-auto'>{src}</a></li>)
        sources.push(a)
      } else{
        const a = (<li key={src}>{src}</li>)
        sources.push(a)
      }
    }
    return (
      <ul className='overflow-hidden'>
        {sources}
      </ul>
    )
  }
  return msg.query_sources.reasoning
}

// Custom Image component that fetches images with authentication headers
const AuthenticatedImage: FC<{ src: string; alt: string }> = ({ src, alt }) => {
  const [imageSrc, setImageSrc] = useState<string>("");
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<boolean>(false);

  useEffect(() => {
    const fetchImage = async () => {
      try {
        // Get credentials from sessionStorage (same pattern as Interact.tsx and SideMenu.tsx)
        const creds = sessionStorage.getItem("creds");
        if (!creds) {
          console.error("No credentials found in sessionStorage");
          setError(true);
          setLoading(false);
          return;
        }

        console.log("Fetching image:", src);
        console.log("Using credentials:", creds ? "present" : "missing");

        // Fetch image with authentication header
        const response = await fetch(src, {
          headers: {
            Authorization: `Basic ${creds}`,
          },
          credentials: 'include', // Include credentials in CORS requests
        });

        console.log("Image fetch response status:", response.status);

        if (!response.ok) {
          const errorText = await response.text().catch(() => 'Unknown error');
          console.error(`Failed to load image: ${response.status}`, errorText);
          console.error("Response headers:", Object.fromEntries(response.headers.entries()));
          throw new Error(`Failed to load image: ${response.status} - ${errorText}`);
        }

        // Convert to blob and create object URL
        const blob = await response.blob();
        const objectUrl = URL.createObjectURL(blob);
        setImageSrc(objectUrl);
        setLoading(false);
      } catch (err) {
        console.error("Error loading image:", err);
        setError(true);
        setLoading(false);
      }
    };

    if (src) {
      fetchImage();
    }

    // Cleanup object URL on unmount
    return () => {
      if (imageSrc) {
        URL.revokeObjectURL(imageSrc);
      }
    };
  }, [src]);

  if (loading) {
    return <span className="text-gray-500">Loading image...</span>;
  }

  if (error || !imageSrc) {
    return <span className="text-red-500">Failed to load image</span>;
  }

  return <img src={imageSrc} alt={alt} className="max-w-full h-auto rounded-md" />;
};

export const CustomChatMessage: FC<IChatbotMessageProps> = ({
  message,
}) => {
  const [showResult, setShowResult] = useState<boolean>(false);
  const [showGraphVis, setShowGraphVis] = useState<boolean>(false);
  const [showTableVis, setShowTableVis] = useState<boolean>(false);
  const [traceMessageId, setTraceMessageId] = useState<string | null>(null);
  const [alert, alertDialog] = useAlert();

  // Error handling functions
  const handleShowExplain = () => {
    if (!message.query_sources?.reasoning) {
      return false;
    }
    setShowResult(prev => !prev);
    return true;
  };

  const handleShowGraph = () => {
    if (!message.query_sources?.result) {
      return false;
    }
    setShowGraphVis(prev => !prev);
    return true;
  };

  const handleShowTable = () => {
    // Allow opening the table view on history messages too — the
    // chat-history backend preserves ``query_sources.result``, so
    // there's no reason to deny it just because the message arrived
    // from a reload rather than a fresh answer.
    if (!message.query_sources?.result) {
      return false;
    }
    setShowTableVis(prev => !prev);
    return true;
  };

  // Custom markdown components to handle images with authentication
  const markdownComponents = {
    img: ({ src, alt }: { src?: string; alt?: string }) => {
      if (!src) return null;
      // Check if it's an internal API image that needs authentication
      if (src.startsWith('/ui/image_vertex/')) {
        return <AuthenticatedImage src={src} alt={alt || 'Image'} />;
      }
      // For external images, use regular img tag
      return <img src={src} alt={alt} className="max-w-full h-auto rounded-md" />;
    },
  };

  return (
    <>
      {alertDialog}
      {traceMessageId && (
        <TraceLogs
          messageIdProp={traceMessageId}
          onClose={() => setTraceMessageId(null)}
        />
      )}
      {typeof message === "string" ? (
        <div className="prose dark:prose-invert text-sm max-w-[230px] md:max-w-[80%] mt-7 mb-7">
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{message}</ReactMarkdown>
        </div>
      ) : message.key === null ? (
        message
      ) : (
        <div className="flex flex-col w-full relative">
          <div className="prose dark:prose-invert text-sm w-full mt-7 mb-7">
            {message.response_type === "progress" ? (
              <p className={`graphrag-thinking${message.response_type !== "history" ? " typewriter" : ""}`}>{message.content}</p>
            ) : (
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents} className={message.response_type === "history" ? undefined : "typewriter"}>{message.content}</ReactMarkdown>
            )}
            <RetrieverBadge message={message} />
            <Interactions
              message={message} 
              showExplain={handleShowExplain}
              showTable={handleShowTable}
              showGraph={handleShowGraph}
              onViewTrace={async () => {
                const messageId = message.messageId || message.message_id || "";
                if (!messageId) {
                  await alert("Trace log unavailable: this message has no trace ID.");
                  return;
                }
                // Guard against a missing/invalid creds value. If we send
                // ``Basic null`` (or other unparsable base64), FastAPI's
                // HTTPBasic returns 401 + ``WWW-Authenticate: Basic`` and
                // the browser pops up its native auth dialog. Better to
                // tell the user to sign in again than to flash that popup.
                const creds = sessionStorage.getItem("creds");
                if (!creds) {
                  await alert("Your session has expired. Please log in again.");
                  return;
                }
                // Trace JSON lives under /code/trace_logs inside the
                // graphrag container and is wiped on container recreate.
                // Probe first so we never open an empty dialog when the file is gone.
                try {
                  const probe = await fetch(`/ui/trace/${messageId}`, {
                    method: "GET",
                    headers: { Authorization: `Basic ${creds}` },
                  });
                  if (!probe.ok) {
                    await alert("Trace log not found.");
                    return;
                  }
                } catch (err) {
                  await alert("Failed to reach the trace log endpoint. Please try again.");
                  return;
                }
                setTraceMessageId(messageId);
              }}
            />
          </div>

          {showGraphVis ? (
            <>
              {/* {message.query_sources?.result ? <pre>{JSON.stringify(message.query_sources?.result, null, 2)}</pre> : null} */}
              {/* <pre>{JSON.stringify(message, null, 2)}</pre> */}
              <div className="relative w-full h-[550px] my-10 border border-solid border-[#000]">
                {message.query_sources?.result.edges ? (
                  <KnowledgeGraphPro data={message.query_sources?.result.edges} />
                ) : (
                  <div className="flex items-center justify-center h-full text-gray-500">
                    No graph data available
                  </div>
                )}
              </div>
              {/* <Dialog>
                <DialogTrigger className="absolute top-[200px] left-[20px]"><ImEnlarge2 /></DialogTrigger>
                <DialogContent className="max-w-[1200px] h-[850px]">
                  <DialogHeader>
                    <DialogDescription>
                      <div className="relative w-full h-[800px]">
                        {message.query_sources?.result ? (<KnowledgeGraphPro data={message.query_sources?.result} />) : null}
                      </div>
                    </DialogDescription>
                  </DialogHeader>
                </DialogContent>
              </Dialog> */}
            </>
          ) : null}

          {showTableVis ? (
            <div className="relative w-full h-[550px] my-10 border border-solid border-[#000] my-10 h-auto">
              {message.query_sources?.result ? (
                <KnowledgeTablPro data={message.query_sources?.result} />
              ) : (
                <div className="flex items-center justify-center h-full text-gray-500">
                  No table data available
                </div>
              )}
            </div>
          ) : null}

          {showResult ? (
            <div className="text-[11px] rounded-md bg-[#ececec] dark:bg-shadeA mt-3 p-4 leading-4 relative">
              <strong>Reasoning:</strong><br/>
              {message.query_sources?.reasoning ? (
                getReasoning(message)
              ) : (
                <span className="text-gray-500">No reasoning data available</span>
              )}
              <span
                className="absolute right-2 bottom-1 cursor-pointer"
                onClick={() => setShowResult(false)}
              >
                <IoIosCloseCircleOutline />
              </span>
            </div>
          ) : null}

        </div>
      )}
    </>
  );
};
