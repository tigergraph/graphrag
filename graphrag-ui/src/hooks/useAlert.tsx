import { useState, ReactElement } from "react";
import { AlertDialog } from "@/components/ui/alert-dialog";

interface AlertOptions {
  message: string;
  onClose: () => void;
}

export function useAlert(): [
  (message: string) => Promise<void>,
  ReactElement | null,
  boolean
] {
  const [options, setOptions] = useState<AlertOptions | null>(null);

  const alert = (message: string): Promise<void> =>
    new Promise<void>((resolve) => {
      setOptions({
        message,
        onClose: () => {
          resolve();
          setOptions(null);
        },
      });
    });

  const alertDialog: ReactElement | null = options ? (
    <AlertDialog message={options.message} onClose={options.onClose} />
  ) : null;

  const isOpen = options !== null;

  return [alert, alertDialog, isOpen];
}
