import React from "react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

interface ConfigScopeToggleProps {
  configScope: "global" | "graph";
  selectedGraph: string;
  availableGraphs: string[];
  onScopeChange: (scope: "global" | "graph") => void;
  onGraphChange: (graph: string) => void;
  /** Optional hint rendered below the toggle when graph scope is active and a graph is selected */
  graphSelectedHint?: React.ReactNode;
  /** CSS class for the outer wrapper (e.g. "mb-6") */
  className?: string;
  /** When true, hides the "Edit global defaults" option and forces graph-specific scope */
  graphOnly?: boolean;
}

const ConfigScopeToggle: React.FC<ConfigScopeToggleProps> = ({
  configScope,
  selectedGraph,
  availableGraphs,
  onScopeChange,
  onGraphChange,
  graphSelectedHint,
  className = "mb-6",
  graphOnly = false,
}) => {
  if (availableGraphs.length === 0) return null;

  return (
    <div className={`bg-white dark:bg-shadeA border border-gray-300 dark:border-[#3D3D3D] rounded-lg p-6 ${className}`}>
      <label className="block text-sm font-medium mb-2 text-black dark:text-white">
        Configuration Scope
      </label>
      <div className="flex items-center gap-4">
        {!graphOnly && (
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="configScopeToggle"
              checked={configScope === "global"}
              onChange={() => onScopeChange("global")}
              className="h-4 w-4"
            />
            <span className="text-sm text-black dark:text-white">Edit global defaults</span>
          </label>
        )}
        {!graphOnly && (
          <label className="flex items-center gap-2 cursor-pointer">
            <input
              type="radio"
              name="configScopeToggle"
              checked={configScope === "graph"}
              onChange={() => onScopeChange("graph")}
              className="h-4 w-4"
            />
            <span className="text-sm text-black dark:text-white">Edit graph-specific config for</span>
          </label>
        )}
        {graphOnly && (
          <span className="text-sm text-black dark:text-white">Edit graph-specific config for</span>
        )}
        <Select
          value={selectedGraph}
          disabled={configScope !== "graph"}
          onValueChange={(value) => onGraphChange(value)}
        >
          <SelectTrigger className="w-auto min-w-[16rem] max-w-[28rem] dark:border-[#3D3D3D] dark:bg-background">
            <SelectValue placeholder="Select a graph" />
          </SelectTrigger>
          <SelectContent>
            {availableGraphs.map((graph) => (
              <SelectItem key={graph} value={graph}>
                {graph}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {configScope === "graph" && !selectedGraph && (
        <p className="text-xs text-amber-600 dark:text-amber-400 mt-2">
          Please select a graph to edit its configuration.
        </p>
      )}
      {configScope === "graph" && selectedGraph && graphSelectedHint && (
        <div className="text-xs text-gray-500 dark:text-gray-400 mt-2">
          {graphSelectedHint}
        </div>
      )}
    </div>
  );
};

export default ConfigScopeToggle;
