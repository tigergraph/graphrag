import { safeJson } from "./safeJson";

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

  let conflicts: string[] = [];
  try {
    const checkResp = await fetch(`/ui/${graphName}/uploads/check`, {
      method: "POST",
      headers: { Authorization: creds, "Content-Type": "application/json" },
      body: JSON.stringify({ filenames }),
    });
    if (!checkResp.ok) {
      // Pre-flight endpoint unreachable — fall back so the upload proceeds.
      return "?overwrite=true";
    }
    const data = await safeJson(checkResp);
    conflicts = Array.isArray(data?.conflicts) ? data.conflicts : [];
  } catch {
    return "?overwrite=true";
  }

  if (conflicts.length === 0) {
    return "";
  }

  const one = conflicts.length === 1;
  const bulletList = conflicts.map((n) => `  •  ${n}`).join("\n");
  const replaceChosen = await confirm(
    `${conflicts.length} file${one ? "" : "s"} already ${one ? "exists" : "exist"} on the server:\n\n` +
      `${bulletList}\n\n` +
      `Replace ${one ? "it" : "them"}?\n` +
      `Cancel will abort the upload.`
  );

  if (replaceChosen) {
    return "?overwrite=true";
  }
  return null;
}
