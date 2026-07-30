import { BsGrid } from "react-icons/bs";
import { IoDocumentTextOutline } from "react-icons/io5";
import { FiTerminal } from "react-icons/fi";
import { FiLoader } from "react-icons/fi";
import { IoCartOutline } from "react-icons/io5";
import { FiKey } from "react-icons/fi";
import { IoIosHelpCircleOutline } from "react-icons/io";
import { HiOutlineChatBubbleOvalLeft } from "react-icons/hi2";
import { MdKeyboardArrowDown, MdKeyboardArrowUp } from "react-icons/md";
import { IoIosArrowForward } from "react-icons/io";
import { useTheme } from "@/components/ThemeProvider";
import { safeJson } from "@/utils/safeJson";
import { GoGear } from "react-icons/go";
import { useState, useEffect } from "react";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogClose,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Table,
  TableBody,
  TableCaption,
  TableCell,
  TableFooter,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { IoPencil } from "react-icons/io5";
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group"
import { FaPaperclip } from "react-icons/fa6";
import { useCallback } from "react";
import { conversationManager } from "../actions/ActionProvider";
import { useConfirm } from "@/hooks/useConfirm";
import { useNavigate } from "react-router-dom";

// TODO make dynamic
const WS_HISTORY_URL = "/ui/user";
const WS_CONVO_URL = "/ui/conversation";
// How many conversations to load at a time. Only the visible ones have their
// messages fetched, so a long history can't flood the browser with requests.
const PAGE_SIZE = 10;

const SideMenu = ({
  height,
  setGetConversationId,
  width,
}: {
  height?: string;
  setGetConversationId?: any;
  width?: number;
}) => {
  const getTheme = useTheme().theme;
  // const [conhistory, setConHistory] = useState([]);
  const [conversationId, setConversationId] = useState<any[]>([]);
  const [conversationId2, setConversationId2] = useState<any[]>([]);
  const [newSet, setNewSet] = useState<any[]>([]);
  const [expandedConversations, setExpandedConversations] = useState<Set<string>>(new Set());
  const [activeConversationId, setActiveConversationId] = useState<string | null>(null);
  // Full sorted conversation list (ids + timestamps only, no messages) and how
  // many of them have had their messages loaded so far.
  const [convList, setConvList] = useState<any[]>([]);
  const [loadedCount, setLoadedCount] = useState(0);
  const [loadingMore, setLoadingMore] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [confirm, confirmDialog] = useConfirm();
  // Fade + disable the side menu (conversation list + New Chat) while
  // the chat is streaming an answer, so the user can't unmount Chat by
  // switching conversations mid-response.
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
  const navigate = useNavigate();


  function formatDate(dateString: any) {
    const options = { year: "numeric" as const, month: "long" as const, day: "numeric" as const}
    return new Date(dateString).toLocaleDateString(undefined, options)
  }

  // Fetch the conversation LIST only (ids + timestamps); cheap, one request.
  const fetchConvList = useCallback(async () => {
    const creds = sessionStorage.getItem("auth");
    const username = sessionStorage.getItem("username");
    if (!username || !creds) return [];
    const selectedGraph = sessionStorage.getItem("selectedGraph") || "";
    const settings = {
      method: "GET",
      headers: { Authorization: creds, "Content-Type": "application/json" },
    };
    const items: any[] = [];
    let cursor = "";
    do {
      const params = new URLSearchParams({ limit: "50" });
      if (selectedGraph) params.set("graph_name", selectedGraph);
      if (cursor) params.set("cursor", cursor);
      const response = await fetch(
        `${WS_HISTORY_URL}/${encodeURIComponent(username)}?${params.toString()}`,
        settings,
      );
      if (!response.ok) return [];
      const page = await safeJson(response);
      if (!Array.isArray(page)) return [];
      items.push(...page);
      cursor = response.headers.get("X-Next-Cursor") || "";
    } while (cursor);
    if (items.length === 0) return [];
    // Most recently updated first (falls back to create_ts).
    return items.sort((a: any, b: any) => {
      const timeA = new Date(a.update_ts || a.create_ts).getTime();
      const timeB = new Date(b.update_ts || b.create_ts).getTime();
      return timeB - timeA;
    });
  }, []);

  // Load message content for a small batch of list items (the only place that
  // hits /ui/conversation/<id>) — bounded to PAGE_SIZE so it can't flood.
  const loadDetails = useCallback(async (items: any[]) => {
    const creds = sessionStorage.getItem("auth");
    const selectedGraph = sessionStorage.getItem("selectedGraph") || "";
    const settings = {
      method: "GET",
      headers: { Authorization: creds!, "Content-Type": "application/json" },
    };
    const results = await Promise.all(
      items.map(async (item: any) => {
        try {
          const content: any[] = [];
          let cursor = "";
          do {
            const params = new URLSearchParams({ limit: "200" });
            if (selectedGraph) params.set("graph_name", selectedGraph);
            if (cursor) params.set("cursor", cursor);
            const r = await fetch(
              `${WS_CONVO_URL}/${encodeURIComponent(item.conversation_id)}?${params.toString()}`,
              settings,
            );
            if (!r.ok) return null;
            const page = await safeJson(r);
            if (!Array.isArray(page)) return null;
            content.push(...page);
            cursor = r.headers.get("X-Next-Cursor") || "";
          } while (cursor);
          let lastUpdateTime = item.update_ts || item.create_ts;
          if (Array.isArray(content) && content.length > 0) {
            const times = content
              .map((m: any) => m.create_ts || m.update_ts)
              .filter((t: any) => t != null)
              .map((t: any) => new Date(t).getTime());
            if (times.length > 0) lastUpdateTime = new Date(Math.max(...times)).toISOString();
          }
          return {
            conversation_id: item.conversation_id,
            content,
            date: formatDate(item.create_ts),
            create_ts: item.create_ts,
            update_ts: lastUpdateTime,
          };
        } catch (error) {
          return null;
        }
      })
    );
    return results.filter((c) => c !== null);
  }, []);

  // Initial / refresh load: latest PAGE_SIZE conversations only.
  const fetchHistory2 = useCallback(async () => {
    try {
      const list = await fetchConvList();
      setConvList(list);
      const firstBatch = list.slice(0, PAGE_SIZE);
      const details = await loadDetails(firstBatch);
      setConversationId(details as any);
      setLoadedCount(firstBatch.length);
    } catch (error) {
      setConversationId([]);
      setConvList([]);
      setLoadedCount(0);
    }
  }, [fetchConvList, loadDetails]);

  // "more…": load the next PAGE_SIZE conversations' messages and append.
  const loadMore = useCallback(async () => {
    if (loadingMore) return;
    setLoadingMore(true);
    try {
      const nextBatch = convList.slice(loadedCount, loadedCount + PAGE_SIZE);
      const details = await loadDetails(nextBatch);
      setConversationId((prev: any[]) => [...prev, ...(details as any[])]);
      setLoadedCount((c) => c + nextBatch.length);
    } finally {
      setLoadingMore(false);
    }
  }, [convList, loadedCount, loadingMore, loadDetails]);

  // Delete the older (not-yet-loaded) conversations. Done in small concurrent
  // batches so clearing a long history can't itself flood the browser.
  const clearOlder = useCallback(async () => {
    const n = convList.length - loadedCount;
    if (n <= 0) return;
    const ok = await confirm(
      `Delete ${n} older conversation${n === 1 ? "" : "s"}?\n\n` +
        `This permanently removes them and cannot be undone.`
    );
    if (!ok) return;
    setClearing(true);
    try {
      const creds = sessionStorage.getItem("auth");
      const settings = {
        method: "DELETE",
        headers: { Authorization: creds!, "Content-Type": "application/json" },
      };
      const older = convList.slice(loadedCount);
      const BATCH = 5;
      for (let i = 0; i < older.length; i += BATCH) {
        const chunk = older.slice(i, i + BATCH);
        await Promise.all(
          chunk.map((c: any) =>
            fetch(`${WS_CONVO_URL}/${c.conversation_id}`, settings).catch(() => {})
          )
        );
      }
      setConvList((prev: any[]) => prev.slice(0, loadedCount));
    } finally {
      setClearing(false);
    }
  }, [convList, loadedCount, confirm]);

  const handleNewChat = () => {
    conversationManager.startNewConversation();
    // Clear any selected conversation data
    sessionStorage.removeItem('selectedConversationData');
    // Force navigation by reloading if already on chat page
    if (window.location.pathname === "/chat") {
      window.location.reload();
    } else {
      navigate("/chat");
    }
  };

  // eslint-disable-next-line
  // @ts-ignore
  const resumeConvo = async (id):any => {
    try {
      // Load conversation into conversation manager
      conversationManager.loadConversation(id);

      // Set as active conversation and expand it
      setActiveConversationId(id);
      setExpandedConversations(prev => new Set([...prev, id]));

      // Store conversation data for the chat component
      const creds = sessionStorage.getItem("auth");
      if (!creds) {
        return;
      }

      const settings = {
        method: 'GET',
        headers: {
          Authorization: creds!,
          "Content-Type": "application/json",
        }
      }

      const selectedGraph = sessionStorage.getItem("selectedGraph") || "";
      const data: any[] = [];
      let cursor = "";
      do {
        const params = new URLSearchParams({ limit: "200" });
        if (selectedGraph) params.set("graph_name", selectedGraph);
        if (cursor) params.set("cursor", cursor);
        const response = await fetch(
          `${WS_CONVO_URL}/${encodeURIComponent(id)}?${params.toString()}`,
          settings,
        );
        if (!response.ok) {
          return;
        }
        const page = await safeJson(response);
        if (!Array.isArray(page)) {
          return;
        }
        data.push(...page);
        cursor = response.headers.get("X-Next-Cursor") || "";
      } while (cursor);
      setConversationId2(data);

      // Store the conversation data in sessionStorage for the chat component
      sessionStorage.setItem('selectedConversationData', JSON.stringify(data));

      // Force reload to restart the WebSocket connection with the conversation ID
      // This ensures the Bot component re-initializes and loads the conversation messages
      if (window.location.pathname === "/chat") {
        window.location.reload();
      } else {
        navigate("/chat");
      }
    } catch (error) {
      // Silently handle error
    }
  }

  const toggleConversation = (conversationId: string) => {
    setExpandedConversations(prev => {
      const newSet = new Set(prev);
      if (newSet.has(conversationId)) {
        newSet.delete(conversationId);
      } else {
        newSet.add(conversationId);
      }
      return newSet;
    });
  }

  const renderConvoHistory = () => {
    if (newSet.length === 0) {
      return (
        <div className="mb-[200px] px-6 pt-4">
          <p className="text-sm text-gray-500 dark:text-gray-400">
            No chat history yet. Start a new conversation to see it here.
          </p>
        </div>
      );
    }

    // Group conversations by date
    const groupedByDate = newSet.reduce((acc: Record<string, any[]>, item: any) => {
      const date = item.date;
      if (!acc[date]) {
        acc[date] = [];
      }
      acc[date].push(item);
      return acc;
    }, {} as Record<string, any[]>);

    // Sort dates (most recently updated first) - convert to array and sort by the first conversation's timestamp
    const sortedDates = Object.entries(groupedByDate).sort(([, convsA], [, convsB]) => {
      const timeA = new Date(convsA[0]?.update_ts || convsA[0]?.create_ts || convsA[0]?.date || 0).getTime();
      const timeB = new Date(convsB[0]?.update_ts || convsB[0]?.create_ts || convsB[0]?.date || 0).getTime();
      return timeB - timeA; // Most recently updated first
    });

    return (
      <div className="mb-[200px]">
        {sortedDates.map(([date, conversations]) => {
          return (
            <div key={date}>
              <h4 className="Urbane-Medium text-lg pl-6 pt-5 text-black dark:text-white">
                {date}
              </h4>
              <ul className="menu border-b border-gray-300 dark:border-[#3D3D3D] text-black mx-6">
                {conversations.map((item: any, idx: number) => {
                  const isExpanded = expandedConversations.has(item.conversation_id);
                  const isActive = activeConversationId === item.conversation_id;

                  // Get all user messages for display
                  const userMessages = item.content?.filter((msg: any) => msg.role === "user") || [];
                  const firstUserMessage = userMessages[0];
                  const previewText = firstUserMessage?.content || "No messages";

                  return (
                    <li key={`${item.conversation_id}-${idx}`} className="text-ellipsis">
                      <div className={`${isActive ? 'bg-gray-100 dark:bg-gray-800' : ''} rounded`}>
                        <a 
                          href="#" 
                          className={`flex py-3 my-3 px-3 items-center hover:bg-gray-100 dark:hover:bg-gray-800 rounded cursor-pointer ${isActive ? 'font-medium' : ''}`}
                          onClick={(e) => {
                            e.preventDefault();
                            resumeConvo(item.conversation_id);
                          }}
                        >
                          <HiOutlineChatBubbleOvalLeft className="text-xl mr-3 flex-shrink-0" />
                          <div className="truncate flex-1">{previewText}</div>
                          {userMessages.length > 1 && (
                            <button
                              onClick={(e) => {
                                e.preventDefault();
                                e.stopPropagation();
                                toggleConversation(item.conversation_id);
                              }}
                              className="ml-2 flex-shrink-0 p-1 hover:bg-gray-200 dark:hover:bg-gray-700 rounded"
                            >
                              {isExpanded ? (
                                <MdKeyboardArrowUp className="text-xl" />
                              ) : (
                                <MdKeyboardArrowDown className="text-xl" />
                              )}
                            </button>
                          )}
                        </a>
                        {isExpanded && userMessages.length > 1 && (
                          <div className="px-3 pb-3 ml-8 border-l-2 border-gray-300 dark:border-gray-600">
                            {userMessages.slice(1).map((msg: any, msgIdx: number) => (
                              <div
                                key={msgIdx}
                                className="py-2 text-sm text-gray-600 dark:text-gray-400 truncate cursor-pointer hover:text-gray-900 dark:hover:text-gray-200"
                                onClick={(e) => {
                                  e.preventDefault();
                                  resumeConvo(item.conversation_id);
                                }}
                              >
                                {msg.content}
                              </div>
                            ))}
                          </div>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
            </div>
          );
        })}
        {loadedCount < convList.length && (
          <div className="px-6 py-4 flex items-center justify-between gap-3">
            <button
              onClick={loadMore}
              disabled={loadingMore || clearing}
              className="text-sm text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200 disabled:opacity-50"
            >
              {loadingMore ? "Loading…" : `more… (${convList.length - loadedCount} older)`}
            </button>
            <button
              onClick={clearOlder}
              disabled={clearing}
              className="text-sm text-gray-400 hover:text-red-600 dark:text-gray-500 dark:hover:text-red-400 disabled:opacity-50"
            >
              {clearing ? "Clearing…" : "Clear older"}
            </button>
          </div>
        )}
      </div>
    )
  }


  useEffect(() => {
    fetchHistory2();
  }, [fetchHistory2]);

  // Conversation history is graph-bound, so switching the selected graph
  // immediately replaces the visible history instead of mixing graph scopes.
  useEffect(() => {
    const handleGraphChange = () => {
      setActiveConversationId(null);
      setExpandedConversations(new Set());
      fetchHistory2();
    };
    window.addEventListener("graphrag:selectedGraph", handleGraphChange);
    return () => {
      window.removeEventListener(
        "graphrag:selectedGraph",
        handleGraphChange,
      );
    };
  }, [fetchHistory2]);

  // Refresh history when component becomes visible (user returns to chat page)
  useEffect(() => {
    const handleVisibilityChange = () => {
      if (!document.hidden) {
        fetchHistory2();
      }
    };

    document.addEventListener('visibilitychange', handleVisibilityChange);
    return () => document.removeEventListener('visibilitychange', handleVisibilityChange);
  }, [fetchHistory2]);

  // Listen for conversation creation/update events to refresh the history
  useEffect(() => {
    const handleConversationEvent = () => {
      // Debounce to avoid too many refreshes
      setTimeout(() => {
        fetchHistory2();
      }, 500);
    };

    window.addEventListener('conversationCreated', handleConversationEvent);
    window.addEventListener('conversationUpdated', handleConversationEvent);

    return () => {
      window.removeEventListener('conversationCreated', handleConversationEvent);
      window.removeEventListener('conversationUpdated', handleConversationEvent);
    };
  }, [fetchHistory2]);

  useEffect(() => {
    setGetConversationId(conversationId);
    // Sort by update_ts (most recently updated first), fallback to create_ts
    const sorted = [...conversationId].sort((a, b) => {
      const timeA = new Date(a.update_ts || a.create_ts || a.date).getTime();
      const timeB = new Date(b.update_ts || b.create_ts || b.date).getTime();
      return timeB - timeA; // Most recently updated first
    });
    setNewSet(sorted);
  }, [conversationId])

  // Track active conversation from conversationManager
  useEffect(() => {
    const checkActiveConversation = () => {
      const currentId = conversationManager.getCurrentConversationId();
      if (currentId && currentId !== activeConversationId) {
        setActiveConversationId(currentId);
        // Auto-expand the active conversation
        setExpandedConversations(prev => new Set([...prev, currentId]));
      } else if (!currentId) {
        setActiveConversationId(null);
      }
    };

    // Check immediately
    checkActiveConversation();

    // Check periodically (every 500ms) to catch changes
    const interval = setInterval(checkActiveConversation, 500);

    return () => clearInterval(interval);
  }, [activeConversationId]);

  return (
    <div
      className={`hidden md:block overflow-y-auto ${height ? "" : "h-[100vh]"} ${chatStreaming ? "pointer-events-none opacity-50" : ""}`}
      style={{ width: width ?? 320, minWidth: width ?? 320 }}
      aria-disabled={chatStreaming}
      title={chatStreaming ? "Disabled while the chat is generating an answer" : undefined}
    >
      <div className="border-b border-gray-300 dark:border-[#3D3D3D] h-[70px]">
        <div className="flex items-center">
          <img
            src={
              getTheme === "dark" || getTheme === "system"
                ? "./tg-logo-bk2.svg"
                : "./tg-logo.svg"
            }
            className="min-h-[32px] pt-5 pl-5 min-w-[144px]'"
          />
          {/* <Popover>
            <PopoverTrigger className="ml-auto"><GoGear className="text-lg mr-5 mt-4"/></PopoverTrigger>
            <PopoverContent className="flex flex-col">





            <Dialog>
              <DialogTrigger asChild>
                <Button variant="outline">Create Knowledge Graph</Button>
              </DialogTrigger>
              <DialogContent className="sm:max-w-[425px]">
                <DialogHeader>
                  <DialogTitle>Create Knowledge Graph</DialogTitle>
                </DialogHeader>
                <div className="grid gap-4 py-4">
                  <div className="grid grid-cols-4 items-center gap-4">
                    <Input
                      id="filename"
                      defaultValue="Paste a filename or url"
                      className="col-span-4"
                    />
                  </div>
                  <div className="flex mt-5"><FaPaperclip className="mr-2" /> <span>Attach file (html, pdf, txt)</span></div>
                </div>
                <DialogFooter>
                  <Button type="submit">Create</Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>









            <Dialog>
              <DialogTrigger asChild>
                <Button variant="outline">Describe Graph Queries</Button>
              </DialogTrigger>
              <DialogContent className="sm:max-w-[900px]">
                <DialogHeader>
                  <DialogTitle>Describe Graph Queries</DialogTitle>
                </DialogHeader>






                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead className="w-[100px]">Query Name</TableHead>
                      <TableHead>Description</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    <TableRow>
                      <TableCell className="font-medium">find_transactions_unusual_for_merchant</TableCell>
                      <TableCell>This query reports transactions having...</TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">find_transactions_unusual_for_card</TableCell>
                      <TableCell>This query reports transactions having...</TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">find_transactions_unusual_velocity</TableCell>
                      <TableCell>[no description yet]</TableCell>
                    </TableRow>
                    <TableRow>
                      <TableCell className="font-medium">find_transactions_unusual_velocity</TableCell>
                      <TableCell>[no description yet]</TableCell>
                    </TableRow>
                  </TableBody>
                </Table>



                <DialogFooter>
                  <Button type="submit">Save</Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>








          <Dialog>
            <DialogTrigger asChild>
              <Button variant="outline">Select LLM</Button>
            </DialogTrigger>
            <DialogContent className="sm:max-w-[425px]">
              <DialogHeader>
                <DialogTitle>Select LLM</DialogTitle>
                <DialogDescription>
                  Please choose your AI provider and its Large Language Model. It may affect results you get.  
                </DialogDescription>
              </DialogHeader>

              <Select>
                <SelectTrigger className="w-[180px]">
                  <SelectValue placeholder="Select" />
                </SelectTrigger>
                <SelectContent>
                  <SelectGroup>
                    <SelectLabel>TBD</SelectLabel>
                  </SelectGroup>
                </SelectContent>
              </Select>

              <RadioGroup defaultValue="comfortable">
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value="default" id="r1" />
                  <Label htmlFor="r1">ChatGPT-4o</Label>
                </div>
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value="comfortable" id="r2" />
                  <Label htmlFor="r2">ChatGPT-4</Label>
                </div>
                <div className="flex items-center space-x-2">
                  <RadioGroupItem value="compact" id="r3" />
                  <Label htmlFor="r3">ChatGPT-3.5</Label>
                </div>
              </RadioGroup>


              <DialogFooter>
                <Button type="submit">Save</Button>
              </DialogFooter>
            </DialogContent>
          </Dialog>





            </PopoverContent>
          </Popover>  */}

        </div>
      </div>

      <div 
        className="gradient rounded-lg h-[44px] flex items-center justify-center mx-5 mt-5 text-white cursor-pointer"
        onClick={() => handleNewChat()}
      >
        + New Chat
      </div>

      <h1 className="Urbane-Medium text-lg pl-4 pt-5 text-black dark:text-white flex">
        <img src="./tg-logo-bk.svg" className="mr-3 ml-2" />
        <span>Chat history</span>
      </h1>

      {renderConvoHistory()}

      {confirmDialog}

      {/* <div
        className={`hidden md:block w-[320px] md:max-w-[320px] absolute bg-white dark:bg-background dark:border-[#3D3D3D] rounded-bl-3xl border-t ${height ? "open-dialog-avatar" : "bottom-0"}`}
      >
        <div className="flex justify-center items-center text-sm h-[80px]">
          <div>
            <img src="./avatar.svg" className="h-[42px] w-[42px] mr-4" />
          </div>
          <div className="mr-4">
            Charles P.
            <br />
            Charles.1980@gmail.com
          </div>
          <IoIosArrowForward />
        </div>
      </div> */}
    </div>
  );
};

export default SideMenu;
