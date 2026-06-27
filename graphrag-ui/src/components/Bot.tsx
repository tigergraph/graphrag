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
  const [chatMode, setChatMode] = useState(sessionStorage.getItem("chatMode") || 'agentic');
  const [ragPattern, setRagPattern] = useState(sessionStorage.getItem("ragPattern") || 'auto');
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

    // Default the chat menu to Agent · Auto when nothing is stored yet
    // (also resets any stale pre-2.0 retriever-only selection).
    if (!sessionStorage.getItem("chatMode")) {
      setChatMode("agentic");
      sessionStorage.setItem("chatMode", "agentic");
      setRagPattern("auto");
      sessionStorage.setItem("ragPattern", "auto");
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

  const handleSelectMode = (mode, value) => {
    setChatMode(mode);
    setRagPattern(value);
    sessionStorage.setItem("chatMode", mode);
    sessionStorage.setItem("ragPattern", value);
    navigate("/chat");
  };

  const triggerLabel =
    chatMode === "agentic"
      ? "Agent · " + ragPattern.charAt(0).toUpperCase() + ragPattern.slice(1)
      : "Classic · " + ragPattern;

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
                  {triggerLabel} <MdKeyboardArrowDown className="text-2xl" />
                </Button>
              </DropdownMenuTrigger>

              <DropdownMenuContent className="w-72">
                <DropdownMenuLabel className="flex items-center gap-2 px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  <span className="text-sm">🤖</span> Agent
                </DropdownMenuLabel>
                <DropdownMenuGroup>
                  {[
                    ["Auto", "auto", "Use the graph's configured strategy"],
                    ["Planned", "planned", "Plan all steps up front, then retrieve"],
                    ["Reactive", "reactive", "Decide each step as it goes"],
                  ].map(([label, value, desc]) => {
                    const active = chatMode === "agentic" && ragPattern === value;
                    return (
                      <DropdownMenuItem
                        key={"agent-" + value}
                        onSelect={() => handleSelectMode("agentic", value)}
                        className="flex flex-col items-start gap-0.5 py-2 pl-4 pr-2"
                      >
                        <span className="flex w-full items-center justify-between text-sm">
                          <span className={active ? "font-semibold" : "font-medium"}>{label}</span>
                          {active && <span className="text-xs">✓</span>}
                        </span>
                        <span className="text-xs text-gray-500 dark:text-gray-400">{desc}</span>
                      </DropdownMenuItem>
                    );
                  })}
                </DropdownMenuGroup>
                <DropdownMenuSeparator />
                <DropdownMenuLabel className="flex items-center gap-2 px-2 py-1.5 text-[11px] font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
                  <span className="text-sm">🔍</span> Classic
                </DropdownMenuLabel>
                <DropdownMenuGroup>
                  {[
                    ["Auto", "Auto pick a retriever per question"],
                    ["Similarity Search", "Vector similarity over chunks"],
                    ["Contextual Search", "Similarity plus surrounding chunks"],
                    ["Hybrid Search", "Vector search plus graph traversal"],
                    ["Community Search", "Summaries over graph communities"],
                  ].map(([f, desc]) => {
                    const active = chatMode === "classic" && ragPattern === f;
                    return (
                      <DropdownMenuItem
                        key={"classic-" + f}
                        onSelect={() => handleSelectMode("classic", f)}
                        className="flex flex-col items-start gap-0.5 py-2 pl-4 pr-2"
                      >
                        <span className="flex w-full items-center justify-between text-sm">
                          <span className={active ? "font-semibold" : "font-medium"}>{f}</span>
                          {active && <span className="text-xs">✓</span>}
                        </span>
                        <span className="text-xs text-gray-500 dark:text-gray-400">{desc}</span>
                      </DropdownMenuItem>
                    );
                  })}
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
        <RagPatternContext.Provider value={{ mode: chatMode, pattern: ragPattern }}>
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
