import { ReactElement } from "react";
import { createPortal } from "react-dom";

interface ConfirmDialogProps {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({ message, onConfirm, onCancel }: ConfirmDialogProps) {
  const handleCancel = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    onCancel();
  };

  const handleConfirm = (e: React.MouseEvent) => {
    e.stopPropagation();
    e.preventDefault();
    onConfirm();
  };

  const handleOverlayClick = (e: React.MouseEvent) => {
    // Prevent closing when clicking outside - user must use Cancel or Proceed buttons
    e.stopPropagation();
    e.preventDefault();
  };

  return createPortal(
    <div 
      className="fixed inset-0 bg-black/80 flex items-center justify-center z-[9999]"
      onClick={handleOverlayClick}
      style={{ pointerEvents: 'auto' }}
    >
      <div 
        className="bg-white dark:bg-background p-6 rounded-xl shadow-lg w-96 text-left relative border border-gray-300 dark:border-[#3D3D3D] z-[10000]"
        onClick={(e) => e.stopPropagation()}
        style={{ pointerEvents: 'auto' }}
      >
        <p className="mb-4 text-black dark:text-white text-left whitespace-pre-line">{message}</p>
        <div className="flex justify-center gap-2">
          <button
            className="px-4 py-2 bg-gray-200 dark:bg-gray-700 text-black dark:text-white rounded hover:bg-gray-300 dark:hover:bg-gray-600 transition-colors cursor-pointer"
            onClick={handleCancel}
            type="button"
            style={{ pointerEvents: 'auto' }}
          >
            Cancel
          </button>
          <button
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 transition-colors cursor-pointer"
            onClick={handleConfirm}
            type="button"
            style={{ pointerEvents: 'auto' }}
          >
            Proceed
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

