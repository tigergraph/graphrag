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
import { useNavigate } from "react-router-dom";

// TODO make dynamic
const WS_HISTORY_URL = "/ui/user";
const WS_CONVO_URL = "/ui/conversation";

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


  const fetchHistory2 = useCallback(async () => {
    setConversationId([]);
    const creds = sessionStorage.getItem("creds");
    const username = sessionStorage.getItem("username");

    if (!username) {
      return;
    }

    if (!creds) {
      return;
    }

    const settings = {
      method: 'GET',
      headers: {
        Authorization: `Basic ${creds}`,
        "Content-Type": "application/json",
      }
    }
    try {
      const response = await fetch(`${WS_HISTORY_URL}/${username}`, settings);

      if (!response.ok) {
        setConversationId([]);
        return;
      }

      const data = await safeJson(response);

      if (!Array.isArray(data) || data.length === 0) {
        setConversationId([]);
        return;
      }

      // Sort conversations by update_ts (most recently updated first), fallback to create_ts
      const sortedData = [...data].sort((a: any, b: any) => {
        // Use update_ts if available, otherwise use create_ts
        const timeA = new Date(a.update_ts || a.create_ts).getTime();
        const timeB = new Date(b.update_ts || b.create_ts).getTime();
        return timeB - timeA; // Most recently updated first
      });

      // Wait for all conversation details to be fetched
      const conversationPromises = sortedData.map(async (item: any) => {
        try {
          const response2 = await fetch(`${WS_CONVO_URL}/${item.conversation_id}`, settings);
          if (!response2.ok) {
            return null;
          }
          const content = await safeJson(response2);

          // Get the most recent message timestamp for sorting
          let lastUpdateTime = item.update_ts || item.create_ts;
          if (Array.isArray(content) && content.length > 0) {
            // Find the most recent message timestamp
            const messageTimes = content
              .map((msg: any) => msg.create_ts || msg.update_ts)
              .filter((ts: any) => ts != null)
              .map((ts: any) => new Date(ts).getTime());
            if (messageTimes.length > 0) {
              const latestMessageTime = Math.max(...messageTimes);
              lastUpdateTime = new Date(latestMessageTime).toISOString();
            }
          }

          return {
            conversation_id: item.conversation_id,
            content: content,
            date: formatDate(item.create_ts),
            create_ts: item.create_ts,
            update_ts: lastUpdateTime // Use for sorting by most recent activity
          };
        } catch (error) {
          return null;
        }
      });

      const conversations = await Promise.all(conversationPromises);
      // Filter out any null values from failed requests
      const validConversations = conversations.filter(conv => conv !== null);
      setConversationId(validConversations as any);
    } catch (error) {
      setConversationId([]);
    }
  }, []);

  const formatDate = (dateString) => {
    const options = { year: "numeric" as const, month: "long" as const, day: "numeric" as const}
    return new Date(dateString).toLocaleDateString(undefined, options)
  }

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
      const creds = sessionStorage.getItem("creds");
      if (!creds) {
        return;
      }

      const settings = {
        method: 'GET',
        headers: {
          Authorization: `Basic ${creds}`,
          "Content-Type": "application/json",
        }
      }

      const response = await fetch(`${WS_CONVO_URL}/${id}`, settings);
      if (!response.ok) {
        return;
      }

      const data = await safeJson(response);
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
      </div>
    )
  }


  useEffect(() => {
    fetchHistory2();
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
