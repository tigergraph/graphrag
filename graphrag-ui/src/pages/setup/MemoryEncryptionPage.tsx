import { useEffect, useState } from "react";
import { KeyRound } from "lucide-react";
import ConfigScopeToggle from "@/components/ConfigScopeToggle";
import MemoryEncryption from "@/components/MemoryEncryption";

const MemoryEncryptionPage = () => {
  const [selectedGraph, setSelectedGraph] = useState(sessionStorage.getItem("selectedGraph") || "");
  const [availableGraphs, setAvailableGraphs] = useState<string[]>([]);
  const [scope, setScope] = useState<"global" | "graph">("global");

  useEffect(() => {
    const site = JSON.parse(sessionStorage.getItem("site") || "{}");
    setAvailableGraphs(site.graphs || []);
  }, []);

  return (
    <div className="p-8">
      <div className="max-w-5xl mx-auto">
        <div className="mb-8">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-12 h-12 rounded-full bg-tigerOrange/10 flex items-center justify-center">
              <KeyRound className="h-6 w-6 text-tigerOrange" />
            </div>
            <div>
              <h1 className="text-2xl font-bold text-black dark:text-white">Memory Encryption</h1>
              <p className="text-sm text-gray-600 dark:text-[#D9D9D9]">
                Data keys encrypt the agent memory and chat history stored on disk. The global key, or a graph key, protects the data keys.
              </p>
            </div>
          </div>
        </div>

        <ConfigScopeToggle
          configScope={scope}
          selectedGraph={selectedGraph}
          availableGraphs={availableGraphs}
          onScopeChange={setScope}
          onGraphChange={(value) => {
            setScope("graph");
            setSelectedGraph(value);
            sessionStorage.setItem("selectedGraph", value);
            window.dispatchEvent(new Event("graphrag:selectedGraph"));
          }}
        />

        <MemoryEncryption scope={scope} graph={selectedGraph} showHeader={false} />
      </div>
    </div>
  );
};

export default MemoryEncryptionPage;
