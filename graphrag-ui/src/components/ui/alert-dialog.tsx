import { createPortal } from "react-dom";

interface AlertDialogProps {
  message: string;
  onClose: () => void;
}

export function AlertDialog({ message, onClose }: AlertDialogProps) {
  const handleClose = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    onClose();
  };

  const handleOverlayClick = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
  };

  return createPortal(
    <div
      className="fixed inset-0 bg-black/80 flex items-center justify-center z-[9999]"
      onClick={handleOverlayClick}
      style={{ pointerEvents: "auto" }}
    >
      <div
        className="bg-white dark:bg-background p-6 rounded-xl shadow-lg w-96 text-left relative border border-gray-300 dark:border-[#3D3D3D] z-[10000]"
        onClick={(e) => e.stopPropagation()}
        style={{ pointerEvents: "auto" }}
      >
        <p className="mb-4 text-black dark:text-white text-center whitespace-pre-line">
          {message}
        </p>
        <div className="flex justify-center">
          <button
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors cursor-pointer"
            onClick={handleClose}
            type="button"
            style={{ pointerEvents: "auto" }}
          >
            OK
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
