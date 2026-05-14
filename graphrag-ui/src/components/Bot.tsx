import "react-chatbot-kit/build/main.css";
import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import Chatbot from "react-chatbot-kit";
import ActionProvider from "../actions/ActionProvider.js";
import config from "../actions/config.js";
import MessageParser from "../actions/MessageParser.js";
import { MdKeyboardArrowDown } from "react-icons/md";
import { SelectedGraphContext, RagPatternContext } from './Contexts.js';

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

const Bot = ({ layout, getConversationId }: { layout?: string | undefined, getConversationId?:any }) => {
  const [store, setStore] = useState<any>();
  const [currentDate, setCurrentDate] = useState('');
  const [selectedGraph, setSelectedGraph] = useState(sessionStorage.getItem("selectedGraph") || '');
  const [ragPattern, setRagPattern] = useState(sessionStorage.getItem("ragPattern") || '');
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    // Function to load store from sessionStorage
    const loadStore = () => {
      const parseStore = JSON.parse(sessionStorage.getItem("site") || "{}");
      setStore(parseStore);
      return parseStore;
    };

    // Initial load
    const parseStore = loadStore();

    // Validate selectedGraph against the current graph list
    const storedGraph = sessionStorage.getItem("selectedGraph");
    const availableGraphs = parseStore?.graphs || [];
    if (!storedGraph || !availableGraphs.includes(storedGraph)) {
      if (availableGraphs.length > 0) {
        const firstGraph = availableGraphs[0];
        setSelectedGraph(firstGraph);
        sessionStorage.setItem("selectedGraph", firstGraph);
      } else {
        setSelectedGraph('');
        sessionStorage.removeItem("selectedGraph");
      }
    }

    // Set default ragPattern if no value in sessionStorage. "Auto" lets the
    // backend RetrieverSelector pick a method per question.
    if (!sessionStorage.getItem("ragPattern")) {
      setRagPattern("Auto");
      sessionStorage.setItem("ragPattern", "Auto");
    }

    const date = new Date();
    const options: Intl.DateTimeFormatOptions = { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long' };
    const formattedDate = date.toLocaleDateString('en-US', options);
    setCurrentDate(formattedDate);

    // Update graph list when window gets focus (when navigating back from Setup)
    const handleFocus = () => {
      loadStore();
    };

    window.addEventListener('focus', handleFocus);

    // Stay in sync when another component (Refresh dialog, Ingest
    // dialog, Customize Prompts) changes the shared selectedGraph.
    const handleSelectedGraph = () => {
      const next = sessionStorage.getItem("selectedGraph") || '';
      if (next !== selectedGraph) setSelectedGraph(next);
    };
    window.addEventListener('graphrag:selectedGraph', handleSelectedGraph);

    // Cleanup
    return () => {
      window.removeEventListener('focus', handleFocus);
      window.removeEventListener('graphrag:selectedGraph', handleSelectedGraph);
    };
  }, []);

  // Reload graph list when navigating back to chat (location change)
  useEffect(() => {
    const parseStore = JSON.parse(sessionStorage.getItem("site") || "{}");
    setStore(parseStore);
  }, [location]);

  const handleSelect = (value) => {
    setSelectedGraph(value);
    sessionStorage.setItem("selectedGraph", value);
    window.dispatchEvent(new Event("graphrag:selectedGraph"));
    navigate("/chat");
    //window.location.reload();
  };

  const handleSelectRag = (value) => {
    setRagPattern(value);
    sessionStorage.setItem("ragPattern", value);
    navigate("/chat");
    //window.location.reload();
  };

  return (
    <div className={layout}>
      {/* {layout === "fp" && ( */}
        <div className="border-b border-gray-300 dark:border-[#3D3D3D] h-[70px] flex items-center bg-white dark:bg-background z-50 rounded-tr-lg px-5">
          <div className="text-sm mr-8">{currentDate}</div>

          <div className="flex gap-4 mr-auto">
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="outline"
                  className="!h-[48px] !outline-b !outline-gray-300 dark:!outline-[#3D3D3D] h-[70px] flex justify-end items-center bg-white dark:bg-background z-50 rounded-tr-lg"
                >
                  <img src="/graph-icon.svg" alt="" className="mr-2" />
                  {ragPattern} <MdKeyboardArrowDown className="text-2xl" />
                </Button>
              </DropdownMenuTrigger>

              <DropdownMenuContent className="w-56">
                <DropdownMenuLabel>Select a GraphRAG Pattern</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuGroup>
                  {["Auto", "Similarity Search", "Contextual Search", "Hybrid Search", "Community Search"].map((f, i) => (
                    <DropdownMenuItem key={i} onSelect={() => handleSelectRag(f)}>
                      {/* <User className="mr-2 h-4 w-4" /> */}
                      <span>{f}</span>
                      {/* <DropdownMenuShortcut>⇧⌘P</DropdownMenuShortcut> */}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuGroup>
              </DropdownMenuContent>
            </DropdownMenu>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="outline"
                  className="!h-[48px] !outline-b !outline-gray-300 dark:!outline-[#3D3D3D] h-[70px] flex justify-end items-center bg-white dark:bg-background z-50 rounded-tr-lg"
                >
                  <img src="/graph-icon.svg" alt="" className="mr-2" />
                  {selectedGraph || <span className="text-gray-400 italic">No Knowledge Graph</span>} <MdKeyboardArrowDown className="text-2xl" />
                </Button>
              </DropdownMenuTrigger>

            <DropdownMenuContent className="min-w-[14rem] max-w-[32rem]">
              <DropdownMenuLabel>Select a KnowledgeGraph</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuGroup>
                {store?.graphs?.length > 0 ? (
                  store.graphs.map((f, i) => (
                    <DropdownMenuItem key={i} onSelect={() => handleSelect(f)}>
                      <span className="truncate">{f}</span>
                    </DropdownMenuItem>
                  ))
                ) : (
                  <DropdownMenuItem disabled>
                    <span className="text-gray-400 italic text-sm">
                      Please create a Knowledge Graph in Setup first
                    </span>
                  </DropdownMenuItem>
                )}
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
          </div>
        </div>
      
      <SelectedGraphContext.Provider value={selectedGraph}>
        <RagPatternContext.Provider value={ragPattern}>
          <Chatbot
            // eslint-disable-next-line
            // @ts-ignore
            config={config}
            fullPage={layout}
            getConversationId={getConversationId}
            messageParser={MessageParser}
            actionProvider={ActionProvider}
          />
        </RagPatternContext.Provider>
      </SelectedGraphContext.Provider>
    </div>
  );
};

export default Bot;
