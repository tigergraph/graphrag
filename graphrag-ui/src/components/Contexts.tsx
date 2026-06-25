import React, { createContext } from "react";

export const SelectedGraphContext = createContext("");
// Chat engine selection: mode ("agentic" | "classic") + the single menu value
// (agent style when agentic, retriever when classic).
export const RagPatternContext = createContext<{ mode: string; pattern: string }>({
  mode: "agentic",
  pattern: "auto",
});