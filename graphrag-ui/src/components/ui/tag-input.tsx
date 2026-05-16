import React, { useState, KeyboardEvent } from "react";

export interface TypeHint {
  name: string;
  description: string;
  // Edge variants only: ``Name (From -> To)`` captures the endpoint
  // pair the user has in mind. Vertex chips leave these undefined.
  fromType?: string;
  toType?: string;
}

interface TagInputProps {
  values: TypeHint[];
  onChange: (values: TypeHint[]) => void;
  placeholder?: string;
  disabled?: boolean;
  ariaLabel?: string;
  // When true, the parser accepts ``Name (From -> To)`` (and the
  // unicode ``→``) before the optional ``: description``. Used on the
  // Suggested Edge Types row only — vertex chips never carry
  // endpoints.
  acceptsEndpoints?: boolean;
  // Map of lowercased names to a human-readable reason for rejection.
  // Lets the parent reject GSQL reserved words, GraphRAG structural
  // types, etc., with a clear message instead of a silent drop later.
  forbiddenNames?: Record<string, string>;
}

// Vertex format: ``Name`` or ``Name: description``.
const VERTEX_RE = /^([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*(.*))?\s*$/;
// Edge format: ``Name``, ``Name (From -> To)``, ``Name: description``,
// or ``Name (From -> To): description``. ``->`` and unicode ``→`` both
// accepted as the arrow.
const EDGE_RE =
  /^([A-Za-z_][A-Za-z0-9_]*)\s*(?:\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*(?:->|→)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\))?\s*(?::\s*(.*))?\s*$/;

const MAX_DESC_DISPLAY = 32;

const formatChip = (hint: TypeHint): string => {
  let label = hint.name;
  if (hint.fromType && hint.toType) {
    label += ` (${hint.fromType} → ${hint.toType})`;
  }
  if (!hint.description) return label;
  const desc =
    hint.description.length > MAX_DESC_DISPLAY
      ? hint.description.slice(0, MAX_DESC_DISPLAY - 1) + "…"
      : hint.description;
  return `${label}: ${desc}`;
};

export const TagInput: React.FC<TagInputProps> = ({
  values,
  onChange,
  placeholder,
  disabled,
  ariaLabel,
  acceptsEndpoints = false,
  forbiddenNames,
}) => {
  const [draft, setDraft] = useState("");
  const [error, setError] = useState<string | null>(null);

  const reasonFor = (name: string): string | null => {
    if (!forbiddenNames) return null;
    return forbiddenNames[name.toLowerCase()] || null;
  };

  const commit = () => {
    const text = draft.trim();
    if (!text) return;
    const re = acceptsEndpoints ? EDGE_RE : VERTEX_RE;
    const m = re.exec(text);
    if (!m) {
      setError(
        acceptsEndpoints
          ? `"${text}" is not valid. Use \`Name\`, \`Name: description\`, \`Name (From -> To)\`, or \`Name (From -> To): description\`.`
          : `"${text}" is not valid. Use \`Name\` or \`Name: description\` (names must start with a letter or underscore).`
      );
      return;
    }
    const name = m[1];
    const fromType = acceptsEndpoints ? (m[2] || "").trim() : "";
    const toType = acceptsEndpoints ? (m[3] || "").trim() : "";
    const descriptionIdx = acceptsEndpoints ? 4 : 2;
    const description = (m[descriptionIdx] || "").trim();

    // Reject reserved/structural names — for every name reference
    // (the type name itself + the optional endpoint vertex types).
    for (const candidate of [name, fromType, toType].filter(Boolean)) {
      const reason = reasonFor(candidate);
      if (reason) {
        setError(`"${candidate}" cannot be used: ${reason}`);
        return;
      }
    }

    if (values.some((v) => v.name.toLowerCase() === name.toLowerCase())) {
      setError(`"${name}" is already in the list.`);
      return;
    }

    onChange([
      ...values,
      {
        name,
        description,
        ...(fromType ? { fromType } : {}),
        ...(toType ? { toType } : {}),
      },
    ]);
    setDraft("");
    setError(null);
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter" || e.key === ",") {
      e.preventDefault();
      commit();
    } else if (e.key === "Backspace" && draft === "" && values.length > 0) {
      // Remove last chip on backspace from empty input.
      onChange(values.slice(0, -1));
    }
  };

  const remove = (idx: number) => {
    onChange(values.filter((_, i) => i !== idx));
  };

  return (
    <div>
      <div
        className={`flex flex-wrap items-center gap-1 min-h-9 px-2 py-1 border rounded text-sm dark:border-[#3D3D3D] dark:bg-shadeA ${
          disabled ? "opacity-50" : ""
        }`}
        aria-label={ariaLabel}
      >
        {values.map((v, i) => (
          <span
            key={`${v.name}-${i}`}
            title={
              v.description
                ? `${v.name}${
                    v.fromType && v.toType ? ` (${v.fromType} → ${v.toType})` : ""
                  }: ${v.description}`
                : `${v.name}${
                    v.fromType && v.toType ? ` (${v.fromType} → ${v.toType})` : ""
                  }`
            }
            className="inline-flex items-center gap-1 px-2 py-0.5 rounded bg-blue-100 dark:bg-blue-900/40 text-xs text-blue-900 dark:text-blue-100"
          >
            <span className="font-mono">{formatChip(v)}</span>
            {!disabled && (
              <button
                type="button"
                onClick={() => remove(i)}
                className="text-blue-700 dark:text-blue-200 hover:text-red-600"
                aria-label={`Remove ${v.name}`}
              >
                ×
              </button>
            )}
          </span>
        ))}
        <input
          type="text"
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
            if (error) setError(null);
          }}
          onKeyDown={handleKeyDown}
          onBlur={commit}
          placeholder={values.length === 0 ? placeholder : ""}
          disabled={disabled}
          className="flex-1 min-w-[120px] bg-transparent outline-none text-sm py-0.5"
        />
      </div>
      {error && (
        <p className="text-xs text-red-600 mt-1">{error}</p>
      )}
    </div>
  );
};

export default TagInput;
