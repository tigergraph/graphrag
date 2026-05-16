/**
 * Safely parse a fetch Response as JSON.
 * If the body is not valid JSON (e.g. an HTML error page from nginx),
 * returns a synthetic error object so callers never crash on `.json()`.
 */
export async function safeJson(response: Response): Promise<any> {
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    return {
      error: true,
      detail: response.ok
        ? "Received an invalid response from the server."
        : `Server error (HTTP ${response.status}): ${response.statusText}`,
    };
  }
}
