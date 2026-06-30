import React, {useState, useRef, useCallback, useEffect, useContext} from 'react';
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
  LIKE,
  DISLIKE,
}
export interface Message {
  conversationId: string;
  messageId: string;
  parentId: string;
  modelName: string;
  content: string;
  answered_question: boolean;
  response_type: string;
  query_sources: any;
  role: string;
  feedback: Feedback;
  comment: string;
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
export { conversationManager };

const ActionProvider: React.FC<ActionProviderProps> = ({
  createChatBotMessage,
  setState,
  children,
}) => {
  const selectedGraph = useContext(SelectedGraphContext);
  const { mode: selectedMode, pattern: selectedRagPattern } = useContext(RagPatternContext);
  const lastUserQueryRef = useRef<string>("");
  // Set true when the user hits Stop, so late messages from the aborted task
  // are ignored; reset when the next question is sent.
  const abortedRef = useRef<boolean>(false);
  const WS_URL = selectedGraph
    ? "/ui/" + selectedGraph + "/chat?rag_pattern=" +
      encodeURIComponent(selectedRagPattern) + "&mode=" + encodeURIComponent(selectedMode)
    : null;
  const [messageHistory, setMessageHistory] = useState<MessageEvent<Message>[]>(
    [],
  );
  // Don't open the socket until a graph is selected — avoids the
  // ws://…/ui//chat connect/1006/reconnect churn on a fresh login.
  const { sendMessage, lastMessage, readyState, getWebSocket } = useWebSocket(WS_URL, {
    onOpen: () => {
      // Defensive: the route guard normally ensures ``auth`` is set
      // before the chat page mounts, but idle-timeout expiry mid-session
      // or a logout from another tab can clear it before the WebSocket
      // (re)opens. Without this check we'd send "null" as the auth
      // header and the server would close the WebSocket with no
      // user-actionable message.
      const creds = sessionStorage.getItem("auth");
      if (!creds) {
        console.error("No auth credentials available; redirecting to login");
        alert("Your session has expired. Please log in again.");
        window.location.href = "/";
        return;
      }
      queryGraphragWs2(creds);

      // Send conversation ID (or "new" for new conversation)
      const conversationId = conversationManager.getCurrentConversationId();
      const conversationIdToSend = conversationId || "new";
      console.log("WebSocket connection " + conversationIdToSend + " established to " + WS_URL);
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
  });

  // Initialize conversation manager and load conversation messages
  useEffect(() => {
    const selectedConversationData = sessionStorage.getItem('selectedConversationData');
    if (selectedConversationData) {
      try {
        const data = JSON.parse(selectedConversationData);

        // Handle different data structures
        let messages: any[] = [];
        let conversationId: string | null = null;

        if (Array.isArray(data) && data.length > 0) {
          // Direct array of messages from API
          messages = data;
          conversationId = data[0].conversation_id;
        } else if (data.messages && Array.isArray(data.messages)) {
          // Wrapped in messages property
          messages = data.messages;
          conversationId = data.messages[0]?.conversation_id;
        } else if (data.content && Array.isArray(data.content)) {
          // Wrapped in content property (from fetchHistory2)
          messages = data.content;
          conversationId = data.conversation_id || data.content[0]?.conversation_id;
        }

        if (conversationId) {
          conversationManager.setCurrentConversationId(conversationId);
        }

        // Load conversation messages into the chat UI
        // Sort messages by timestamp if available to maintain chronological order
        const sortedMessages = [...messages].sort((a: any, b: any) => {
          const timeA = a.create_ts ? new Date(a.create_ts).getTime() : 0;
          const timeB = b.create_ts ? new Date(b.create_ts).getTime() : 0;
          return timeA - timeB; // Oldest first
        });

        const loadedMessages: any[] = [];

        sortedMessages.forEach((msg: any) => {
          if (msg.role === "user") {
            // Create user message
            const userMessage = createClientMessage(msg.content || "", {
              delay: 0,
            });
            loadedMessages.push(userMessage);
          } else if (msg.role === "system") {
            // Carry message_id + feedback through so history bubbles can
            // open the trace page and reflect the prior thumbs-up/down
            // state after a reload.
            const botMessage = createChatBotMessage({
              content: msg.content || "",
              response_type: "history",
              query_sources: msg.query_sources,
              answered_question: msg.answered_question,
              message_id: msg.message_id,
              messageId: msg.message_id,
              feedback: msg.feedback,
            });
            loadedMessages.push(botMessage);
          }
        });

        // Set the loaded messages in the chat state
        if (loadedMessages.length > 0) {
          setState((prev: any) => ({
            ...prev,
            messages: loadedMessages,
          }));
        }
      } catch (error) {
        // Silently handle error parsing conversation data
      }
    }
  }, [createChatBotMessage, createClientMessage, setState]);

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
    abortedRef.current = false;  // new question — resume processing messages
    const queryGraphragWsTest = (msg: string) => {
      sendMessage(msg);
    };
    queryGraphragWsTest(msg);
    const loading = createChatBotMessage(<Loader />);
    setState((prev: any) => ({
      ...prev,
      messages: [...prev.messages, loading],
    }));

    // Signal that the chat is now waiting on an answer. Layout chrome
    // (Setup / Logout / conversation list / new-chat button) listens for
    // this and disables itself so the user can't unmount the in-flight
    // streaming connection by navigating away.
    document.body.classList.add("chat-streaming");
    window.dispatchEvent(new Event("chat:streaming-start"));

    // Dispatch event to refresh conversation list when user sends a question
    // This ensures the side menu updates when a new message is sent
    window.dispatchEvent(new CustomEvent('conversationUpdated'));
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
      // After Stop, ignore any buffered/late messages from the aborted task.
      if (abortedRef.current) return;
      setMessageHistory((prev) => prev.concat(lastMessage));

      try {
        const messageData = JSON.parse(lastMessage.data);

        // Check if this is a conversation ID message (first message from backend)
        if (messageData.conversation_id && !messageData.content) {
          conversationManager.setCurrentConversationId(messageData.conversation_id);
          // Don't dispatch refresh event here - refresh happens when user sends the question
          return; // Don't create a bot message for conversation ID
        }

        // One-off engine notice (e.g. Agent mode downgraded to Classic). It
        // arrives before any user turn, so append it without slicing a loader.
        if (messageData.system_note) {
          const noteMessage = createChatBotMessage({
            content: messageData.system_note,
            response_type: "system",
          });
          setState((prev: any) => ({ ...prev, messages: [...prev.messages, noteMessage] }));
          return;
        }

        // Attach the user query so the trace page can display it
        messageData.userQuery = lastUserQueryRef.current;

        // Handle regular bot messages
        const botMessage = createChatBotMessage(messageData);
        setState((prev) => {
          const newPrevMsg = prev.messages.slice(0, -1);
          return {...prev, messages: [...newPrevMsg, botMessage]};
        });

        // Final (non-progress) message ends the streaming gate; layout
        // chrome re-enables. Progress messages keep the gate held.
        if (messageData.response_type !== "progress") {
          document.body.classList.remove("chat-streaming");
          window.dispatchEvent(new Event("chat:streaming-end"));
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
  }, [lastMessage]);

  // Stop button (frontend-only abort). Fired by the Stop control in the input
  // area via a window event. Closes the socket to discard the in-flight
  // streaming response (it auto-reconnects for the next question), replaces the
  // loader with a "Stopped." notice, and re-enables the input. In-flight
  // backend work may still finish in the background; its messages are dropped.
  useEffect(() => {
    const onStop = () => {
      if (!document.body.classList.contains("chat-streaming")) return;
      abortedRef.current = true;
      try { getWebSocket()?.close(); } catch (e) { /* ignore */ }
      const stopped = createChatBotMessage({
        content: "Stopped.",
        response_type: "system",
      });
      setState((prev: any) => {
        const msgs = prev.messages.length ? prev.messages.slice(0, -1) : prev.messages;
        return { ...prev, messages: [...msgs, stopped] };
      });
      document.body.classList.remove("chat-streaming");
      window.dispatchEvent(new Event("chat:streaming-end"));
    };
    window.addEventListener("chat:stop", onStop);
    return () => window.removeEventListener("chat:stop", onStop);
  }, [getWebSocket, createChatBotMessage, setState]);

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
