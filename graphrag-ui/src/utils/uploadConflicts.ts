import { safeJson } from "./safeJson";

interface UploadPreflight {
  conflicts: string[];
  idConflicts: Record<string, string[]>;
}

/**
 * ``POST /uploads/check`` for the planned filenames. Returns ``null``
 * when the pre-flight endpoint is unreachable or errors, so callers can
 * fall back to letting the upload proceed.
 */
async function preflightUpload(
  graphName: string,
  filenames: string[],
  creds: string
): Promise<UploadPreflight | null> {
  try {
    const checkResp = await fetch(`/ui/${graphName}/uploads/check`, {
      method: "POST",
      headers: { Authorization: creds, "Content-Type": "application/json" },
      body: JSON.stringify({ filenames }),
    });
    if (!checkResp.ok) return null;
    const data = await safeJson(checkResp);
    const rawIds = data?.id_conflicts;
    const idConflicts: Record<string, string[]> = {};
    if (rawIds && typeof rawIds === "object") {
      for (const [name, others] of Object.entries(rawIds)) {
        if (Array.isArray(others) && others.length > 0) {
          idConflicts[name] = others.map(String);
        }
      }
    }
    return {
      conflicts: Array.isArray(data?.conflicts) ? data.conflicts : [],
      idConflicts,
    };
  } catch {
    return null;
  }
}

async function confirmReplace(
  conflicts: string[],
  confirm: (message: string) => Promise<boolean>
): Promise<boolean> {
  const one = conflicts.length === 1;
  const bulletList = conflicts.map((n) => `  •  ${n}`).join("\n");
  return confirm(
    `${conflicts.length} file${one ? "" : "s"} already ${one ? "exists" : "exist"} on the server:\n\n` +
      `${bulletList}\n\n` +
      `Replace ${one ? "it" : "them"}?\n` +
      `Cancel will abort the upload.`
  );
}

/**
 * Pre-flight the planned upload to detect filename collisions, then
 * prompt the user once and resolve the chosen action into the query
 * string for the subsequent ``POST /uploads`` (or
 * ``POST /convert_sample_files``) call.
 *
 * Three outcomes:
 *   * No conflicts → ``""`` (just send the upload as-is)
 *   * User chose Replace → ``"?overwrite=true"``
 *   * User cancelled → ``null`` (caller should abort the upload)
 *
 * If the pre-flight endpoint is unreachable or returns an error,
 * falls back to ``"?overwrite=true"`` so the upload still proceeds.
 *
 * ``confirm`` is supplied by the caller (typically the ``useConfirm``
 * hook) so the prompt uses the app's styled dialog instead of the
 * browser default.
 */
export async function resolveUploadConflicts(
  graphName: string,
  filenames: string[],
  creds: string,
  confirm: (message: string) => Promise<boolean>
): Promise<string | null> {
  if (filenames.length === 0) {
    return "";
  }

  const preflight = await preflightUpload(graphName, filenames, creds);
  if (preflight === null) {
    // Pre-flight endpoint unreachable — fall back so the upload proceeds.
    return "?overwrite=true";
  }
  const conflicts = preflight.conflicts;

  if (conflicts.length === 0) {
    return "";
  }

  if (await confirmReplace(conflicts, confirm)) {
    return "?overwrite=true";
  }
  return null;
}

/**
 * Like ``resolveUploadConflicts``, but also reports the planned files
 * that would get the same document as another stored file
 * (``blocked``: file name → clashing stored files). Those files must be
 * left out of the upload; the replace prompt covers only the rest.
 *
 * Returns ``null`` when the user cancelled.
 */
export async function resolveUploadPlan(
  graphName: string,
  filenames: string[],
  creds: string,
  confirm: (message: string) => Promise<boolean>
): Promise<{ query: string; blocked: Record<string, string[]> } | null> {
  if (filenames.length === 0) {
    return { query: "", blocked: {} };
  }
  const preflight = await preflightUpload(graphName, filenames, creds);
  if (preflight === null) {
    return { query: "?overwrite=true", blocked: {} };
  }
  const blocked = preflight.idConflicts;
  const conflicts = preflight.conflicts.filter((n) => !(n in blocked));
  if (conflicts.length === 0) {
    return { query: "", blocked };
  }
  if (await confirmReplace(conflicts, confirm)) {
    return { query: "?overwrite=true", blocked };
  }
  return null;
}
