import React, { useState } from "react";
import { Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

const RestoreApiTokenCard: React.FC<{
  name: string;
  detail: string;
  busy: boolean;
  onSave: (token: string) => Promise<void>;
}> = ({ name, detail, busy, onSave }) => {
  const [token, setToken] = useState("");
  const [error, setError] = useState("");

  return (
    <div className="rounded-md border border-amber-300 bg-amber-50 p-3 dark:border-amber-800 dark:bg-amber-900/20">
      <div className="text-sm font-medium text-amber-950 dark:text-amber-100">
        API token is missing for {name}
      </div>
      <p className="mt-1 text-xs text-amber-800 dark:text-amber-200">
        Enter the token for {detail} to continue ingest.
      </p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Input
          type="password"
          autoComplete="new-password"
          value={token}
          placeholder="API token"
          className="h-8 max-w-sm bg-white dark:bg-background"
          onChange={(event) => setToken(event.target.value)}
        />
        <Button
          size="sm"
          disabled={busy || !token.trim()}
          onClick={async () => {
            setError("");
            try {
              await onSave(token.trim());
              setToken("");
            } catch (saveError: unknown) {
              const message =
                saveError instanceof Error
                  ? saveError.message
                  : "Could not save the token.";
              setError(message);
            }
          }}
        >
          {busy && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
          Save token
        </Button>
      </div>
      {error && <div className="mt-2 text-xs text-red-600">{error}</div>}
    </div>
  );
};

export default RestoreApiTokenCard;
