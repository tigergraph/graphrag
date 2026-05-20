import React, {useState, useRef, useCallback, useEffect, useLayoutEffect, useContext} from 'react';
import {createClientMessage} from 'react-chatbot-kit';
import useWebSocket, {ReadyState} from 'react-use-websocket';
import Loader from '../components/Loader';
import { SelectedGraphContext, RagPatternContext } from '../components/Contexts';

interface ActionProviderProps {
  createChatBotMessage: any;
  setState: any;
  children: any;
}

export enum Feedback {
  NoFeedback = 0,
  LIKE = 1,
  DISLIKE = 2,
}

export interface Message {
  conversationId: string;
  messageId: string;
  message_id?: string;
  parentId: string;
  modelName: string;
  content: string;
  answered_question: boolean;
  response_type: string;
  query_sources: any;
  role: string;
  feedback?: Feedback;
  comment?: string;
}

/** Persist last active TG thread per graph — restored after full page reload. */
const ACTIVE_CONVO_ID_KEY = "graphrag:activeConversationId";
const ACTIVE_CONVO_GRAPH_KEY = "graphrag:activeConversationGraph";

function clearPersistedActiveThread(): void {
  sessionStorage.removeItem(ACTIVE_CONVO_ID_KEY);
  sessionStorage.removeItem(ACTIVE_CONVO_GRAPH_KEY);
}

function persistActiveThread(conversationId: string, graphName: string): void {
  if (!conversationId || !graphName) return;
  sessionStorage.setItem(ACTIVE_CONVO_ID_KEY, conversationId);
  sessionStorage.setItem(ACTIVE_CONVO_GRAPH_KEY, graphName);
}

// Conversation manager functionality
let currentConversationId: string | null = null;
let onNewConversationCallback: (() => void) | null = null;

const conversationManager = {
  // Set the current conversation ID
  setCurrentConversationId: (id: string | null) => {
    currentConversationId = id;
  },

  // Get the current conversation ID
  getCurrentConversationId: (): string | null => {
    return currentConversationId;
  },

  // Register a callback to be called when a new conversation is created
  onNewConversation: (callback: () => void) => {
    onNewConversationCallback = callback;
  },

  // Start a new conversation
  startNewConversation: () => {
    currentConversationId = null;
    clearPersistedActiveThread();
    if (onNewConversationCallback) {
      onNewConversationCallback();
    }
    // Clear conversation data from sessionStorage
    sessionStorage.removeItem('selectedConversationData');
    // Don't reload the page - just clear the chat state
  },

  // Load an existing conversation
  loadConversation: (conversationId: string) => {
    currentConversationId = conversationId;
  },

  // Clear the conversation state
  clearConversation: () => {
    currentConversationId = null;
  }
};

// Export conversation manager for use in other components
export { conversationManager, persistActiveThread };

const ActionProvider: React.FC<ActionProviderProps> = ({
  createChatBotMessage,
  setState,
  children,
}) => {
  const selectedGraph = useContext(SelectedGraphContext);
  const selectedRagPattern = useContext(RagPatternContext);
  const lastUserQueryRef = useRef<string>("");
  const WS_URL = "/ui/" + selectedGraph + "/chat" + "?rag_pattern=" + selectedRagPattern;
  const [messageHistory, setMessageHistory] = useState<MessageEvent<Message>[]>(
    [],
  );

  // Runs before browser paint — must set conversation id before the WebSocket onOpen handshake.
  useLayoutEffect(() => {
    const graph = (selectedGraph || sessionStorage.getItem("selectedGraph") || "").trim();

    const resumeRaw = sessionStorage.getItem("selectedConversationData");
    let cid: string | null = null;

    if (resumeRaw) {
      try {
        const data = JSON.parse(resumeRaw);
        if (Array.isArray(data) && data.length > 0) {
          cid = data[0].conversation_id ?? null;
        } else if (Array.isArray(data.messages) && data.messages?.length > 0) {
          cid = data.messages[0]?.conversation_id ?? null;
        } else if (Array.isArray(data.content) && data.content?.length > 0) {
          cid = data.conversation_id ?? data.content[0]?.conversation_id ?? null;
        }
      } catch {
        /* ignore */
      }
    }

    if (!cid && graph) {
      const ag = sessionStorage.getItem(ACTIVE_CONVO_GRAPH_KEY);
      const aid = sessionStorage.getItem(ACTIVE_CONVO_ID_KEY);
      if (aid && ag === graph) {
        cid = aid;
      }
    }

    if (cid) {
      conversationManager.loadConversation(cid);
    }
  }, [selectedGraph]);

  const { sendMessage, lastMessage, readyState } = useWebSocket(
    WS_URL,
    {
      onOpen: () => {
        const creds = sessionStorage.getItem("creds");
        console.log("Sending credentials, length:", creds ? creds.length : 0);
        queryGraphragWs2(creds!);

        const conversationId = conversationManager.getCurrentConversationId();
        const conversationIdToSend = conversationId || "new";
        console.log(
          "WebSocket connection " + conversationIdToSend + " established to " + WS_URL,
        );
        sendMessage(conversationIdToSend);
      },
      onError: (error) => {
        console.error("WebSocket error:", error);
      },
      onClose: (event) => {
        console.log("WebSocket closed:", event.code, event.reason);
      },
      shouldReconnect: (closeEvent) => {
        console.log("WebSocket should reconnect:", closeEvent.code !== 1000);
        return closeEvent.code !== 1000; // Don't reconnect on normal closure
      },
    },
    Boolean(selectedGraph),
  );

  // Hydrate chat from session (sidebar resume) or fetch last active thread after reload
  useEffect(() => {
    let cancelled = false;

    const applySortedMessages = (sortedMessages: any[]) => {
      const loadedMessages: any[] = [];
      let lastUserContent = "";
      const conversationWindow: { role: string; content: string }[] = [];

      sortedMessages.forEach((msg: any) => {
        if (msg.role === "user") {
          lastUserContent = msg.content || "";
          conversationWindow.push({ role: "user", content: lastUserContent });
          if (conversationWindow.length > 4) conversationWindow.shift();
          const userMessage = createClientMessage(msg.content || "", {
            delay: 0,
          });
          loadedMessages.push(userMessage);
        } else if (msg.role === "system") {
          const userQuery = msg.user_query || lastUserContent || "";
          const botMessage = createChatBotMessage({
            content: msg.content || "",
            response_type: "history",
            query_sources: msg.query_sources ?? {},
            answered_question: msg.answered_question,
            response_time: msg.response_time,
            message_id: msg.message_id,
            messageId: msg.message_id,
            user_query: userQuery,
            userQuery,
            conversation: [...conversationWindow],
          });
          conversationWindow.push({
            role: "assistant",
            content: msg.content || "",
          });
          if (conversationWindow.length > 4) conversationWindow.shift();
          loadedMessages.push(botMessage);
        }
      });

      if (loadedMessages.length > 0) {
        setState((prev: any) => ({
          ...prev,
          messages: loadedMessages,
        }));
      }
    };

    const hydrateFromApiArray = (messages: any[], conversationId: string | null) => {
      if (conversationId) {
        conversationManager.setCurrentConversationId(conversationId);
      }
      const sortedMessages = [...messages].sort((a: any, b: any) => {
        const timeA = a.create_ts ? new Date(a.create_ts).getTime() : 0;
        const timeB = b.create_ts ? new Date(b.create_ts).getTime() : 0;
        return timeA - timeB;
      });
      applySortedMessages(sortedMessages);
    };

    const selectedConversationData = sessionStorage.getItem("selectedConversationData");
    if (selectedConversationData) {
      try {
        const data = JSON.parse(selectedConversationData);

        let messages: any[] = [];
        let conversationId: string | null = null;

        if (Array.isArray(data) && data.length > 0) {
          messages = data;
          conversationId = data[0].conversation_id;
        } else if (data.messages && Array.isArray(data.messages)) {
          messages = data.messages;
          conversationId = data.messages[0]?.conversation_id;
        } else if (data.content && Array.isArray(data.content)) {
          messages = data.content;
          conversationId = data.conversation_id || data.content[0]?.conversation_id;
        }

        // Only short-circuit when we actually restored messages. If session holds
        // stale/empty JSON (truthy string but no rows), fall through so
        // graphrag:activeConversationId + GET /ui/conversation can still hydrate.
        if (messages.length > 0 && conversationId) {
          hydrateFromApiArray(messages, conversationId);
          return () => {
            cancelled = true;
          };
        }
      } catch {
        /* ignore — fall through to TG restore */
      }
    }

    const graph = (selectedGraph || sessionStorage.getItem("selectedGraph") || "").trim();
    const aid = sessionStorage.getItem(ACTIVE_CONVO_ID_KEY);
    const ag = sessionStorage.getItem(ACTIVE_CONVO_GRAPH_KEY);
    if (!graph || !aid || ag !== graph) {
      return () => {
        cancelled = true;
      };
    }

    const creds = sessionStorage.getItem("creds");
    if (!creds) {
      return () => {
        cancelled = true;
      };
    }

    (async () => {
      try {
        const response = await fetch(
          `/ui/conversation/${encodeURIComponent(aid)}?graphname=${encodeURIComponent(graph)}`,
          {
            method: "GET",
            headers: {
              Authorization: `Basic ${creds}`,
              "Content-Type": "application/json",
            },
          },
        );
        if (!response.ok || cancelled) {
          return;
        }
        const data = await response.json();
        if (!Array.isArray(data) || data.length === 0 || cancelled) {
          return;
        }
        const conversationId = data[0].conversation_id ?? aid;
        hydrateFromApiArray(data, conversationId);
      } catch {
        /* ignore */
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [selectedGraph, createChatBotMessage, createClientMessage, setState]);

  // eslint-disable-next-line
  // @ts-ignore
  const queryGraphragWs2 = useCallback((msg: string) => {
    sendMessage(msg);
  });

  const updateState = (message: any) => {
    setState((prev: any) => ({
      ...prev,
      messages: [...prev.messages, message],
    }));
  };

  const updateLastMessage = (_) => {
    setState(prev => ({
      ...prev,
      messages: [...prev.messages.slice(0, 1)]
    }))
  };

  const defaultQuestions = (msg: string) => {
    lastUserQueryRef.current = msg;
    const clientMessage = createClientMessage(msg, {
      delay: 300,
    });
    updateState(clientMessage);
    queryGraphragWs(msg);
  };

  const queryGraphragWs = (msg) => {
    lastUserQueryRef.current = msg;
    const queryGraphragWsTest = (msg: string) => {
      sendMessage(msg);
    };
    queryGraphragWsTest(msg);
    const loading = createChatBotMessage(<Loader />);
    setState((prev: any) => ({
      ...prev,
      messages: [...prev.messages, loading],
    }));

  };

  // FOR REFERENCE
  // const handleTransactionFraud = (msg) => {
  //   console.log(msg);
  //   const clientMessage = createClientMessage(msg, {
  //     delay: 3000,
  //   });
  //   updateState(clientMessage);
  //   const loading = createChatBotMessage(<Loader />);
  //   setState((prev: any) => ({
  //     ...prev,
  //     messages: [...prev.messages, loading],
  //   }));
  //   setTimeout(() => {
  //     const botMessage = createChatBotMessage(
  //       'Transactions refer to the execution of a series of operations or exchanges between two or more parties. They are fundamental to various domains, particularly in economics, finance, and computer science. Here’s a detailed look at transactions in different contexts:',
  //       {
  //         delay: 0,
  //         widget: 'transaction-fraud',
  //       }
  //     );
  //     setState((prev) => {
  //       const newPrevMsg = prev.messages.slice(0, -1);
  //       return {...prev, messages: [...newPrevMsg, botMessage]};    
  //     });
  //   }, 2000);
  // };

  useEffect(() => {
    if (lastMessage !== null) {
      setMessageHistory((prev) => prev.concat(lastMessage));

      try {
        const messageData = JSON.parse(lastMessage.data);

        // Handshake-only payload is {"conversation_id": "..."}; do not confuse with replies that have empty text.
        if (
          messageData.conversation_id &&
          messageData.message_id == null &&
          messageData.messageId == null
        ) {
          conversationManager.setCurrentConversationId(messageData.conversation_id);
          const gHandshake = (
            sessionStorage.getItem("selectedGraph") ||
            selectedGraph ||
            ""
          ).trim();
          if (gHandshake) {
            persistActiveThread(messageData.conversation_id, gHandshake);
          }
          window.dispatchEvent(new CustomEvent("conversationCreated"));
          return;
        }

        // Attach the user query so the trace page can display it
        messageData.userQuery = lastUserQueryRef.current;
        messageData.user_query = lastUserQueryRef.current;

        const isProgressUpdate =
          messageData.response_type === "progress" ||
          messageData.response_type === "PROGRESS";

        // Handle regular bot messages
        const botMessage = createChatBotMessage(messageData);
        setState((prev) => {
          const newPrevMsg = prev.messages.slice(0, -1);
          return {...prev, messages: [...newPrevMsg, botMessage]};  
        });

        // Sidebar lists from TigerGraph after each completed assistant turn (persisted server-side).
        if (
          !isProgressUpdate &&
          (messageData.message_id || messageData.messageId)
        ) {
          const g =
            (sessionStorage.getItem("selectedGraph") || selectedGraph || "").trim();
          if (messageData.conversation_id && g) {
            persistActiveThread(messageData.conversation_id, g);
          }
          window.dispatchEvent(new CustomEvent("conversationUpdated"));
        }
      } catch (error) {
        console.error("Error parsing WebSocket message:", error);
        // Handle string messages (progress updates)
        if (typeof lastMessage.data === 'string') {
          const botMessage = createChatBotMessage({
            content: lastMessage.data,
            response_type: "progress"
          });
      setState((prev) => {
        const newPrevMsg = prev.messages.slice(0, -1);
        return {...prev, messages: [...newPrevMsg, botMessage]};  
      });
        }
      }
    }
  }, [lastMessage, selectedGraph, createChatBotMessage, setState]);

  // FOR REFERENCE
  // const queryGraphrag = async (usrMsg: string) => {
  //   const settings = {
  //     method: 'POST',
  //     body: JSON.stringify({"query": usrMsg}),
  //     headers: {
  //       'Authorization': 'Basic c3VwcG9ydGFpOnN1cHBvcnRhaQ==',
  //       'Accept': 'application/json',
  //       'Content-Type': 'application/json',
  //     }
  //   }
  //   const loading = createChatBotMessage(<Loader />)
  //   setState((prev: any) => ({
  //     ...prev,
  //     messages: [...prev.messages, loading]
  //   }))
  //   const response = await fetch(API_QUERY, settings);
  //   const data = await response.json();
  //   const botMessage = createChatBotMessage(data);
  //   setState((prev) => {
  //     const newPrevMsg = prev.messages.slice(0, -1)
  //     return { ...prev, messages: [...newPrevMsg, botMessage], }
  //   })
  // }

  const connectionStatus = {
    [ReadyState.CONNECTING]: 'Connecting',
    [ReadyState.OPEN]: 'Open',
    [ReadyState.CLOSING]: 'Closing',
    [ReadyState.CLOSED]: 'Closed',
    [ReadyState.UNINSTANTIATED]: 'Uninstantiated',
  }[readyState];

  return (
    <div>
      {/* <span className='absolute bottom-0 pl-2 z-[5000] text-[8px] text-[#666]'>The WebSocket is currently {connectionStatus}</span> */}
      {React.Children.map(children, (child) => {
        return React.cloneElement(child, {
          actions: {
            defaultQuestions,
            // handleTransactionFraud,
            queryGraphragWs,
            updateLastMessage
          },
        });
      })}
    </div>
  );
};

export default ActionProvider;
