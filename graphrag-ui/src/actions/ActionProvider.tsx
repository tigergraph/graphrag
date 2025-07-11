import React, {useState, useCallback, useEffect, useContext} from 'react';
import {createClientMessage} from 'react-chatbot-kit';
import useWebSocket, {ReadyState} from 'react-use-websocket';
import Loader from '../components/Loader';
import { SelectedGraphContext } from '../components/Contexts';

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
    // Clear conversation data from localStorage
    localStorage.removeItem('selectedConversationData');
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
  const WS_URL = "/ui/" + selectedGraph + "/chat";
  const [messageHistory, setMessageHistory] = useState<MessageEvent<Message>[]>(
    [],
  );
  const { sendMessage, lastMessage, readyState } = useWebSocket(WS_URL, {
    onOpen: () => {
      // Send authentication credentials
      queryGraphragWs2(localStorage.getItem("creds")!);
      console.log("WebSocket connection established to " + WS_URL);
      
      // Send RAG pattern
      sendMessage(localStorage.getItem("ragPattern") || "hybridsearch");
      
      // Send conversation ID (or "new" for new conversation)
      const conversationId = conversationManager.getCurrentConversationId();
      const conversationIdToSend = conversationId || "new";
      sendMessage(conversationIdToSend);
    },
  });

  // Initialize conversation manager with any existing conversation data
  useEffect(() => {
    const selectedConversationData = localStorage.getItem('selectedConversationData');
    if (selectedConversationData) {
      try {
        const data = JSON.parse(selectedConversationData);
        // Extract conversation ID from the first message
        if (data.messages && data.messages.length > 0) {
          const conversationId = data.messages[0].conversation_id;
          conversationManager.setCurrentConversationId(conversationId);
        }
      } catch (error) {
        console.error("Error parsing conversation data:", error);
      }
    }
  }, []);

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
    const clientMessage = createClientMessage(msg, {
      delay: 300,
    });
    updateState(clientMessage);
    queryGraphragWs(msg);
  };

  const queryGraphragWs = (msg) => {
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
        
        // Check if this is a conversation ID message (first message from backend)
        if (messageData.conversation_id && !messageData.content) {
          conversationManager.setCurrentConversationId(messageData.conversation_id);
          return; // Don't create a bot message for conversation ID
        }
        
        // Handle regular bot messages
        const botMessage = createChatBotMessage(messageData);
        setState((prev) => {
          const newPrevMsg = prev.messages.slice(0, -1);
          return {...prev, messages: [...newPrevMsg, botMessage]};  
        });
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
