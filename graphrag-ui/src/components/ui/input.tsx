import * as React from "react";

import { cn } from "@/lib/utils";

export interface InputProps
  extends React.InputHTMLAttributes<HTMLInputElement> {}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, style, disabled, ...props }, ref) => {
    // WebKit (Chrome/Safari on macOS) clips the underscore descender when the
    // <input> itself constrains its height (h-10 + py-2). The fix used for the
    // extracted-schema inputs: a sized WRAPPER holds the box (border, height,
    // padding, focus ring) and the inner <input> is borderless, p-0, and not
    // height-constrained, with appearance:none + an explicit line-height. Then
    // the descender renders. Caller className styles the wrapper (widths,
    // borders, bg); caller style still wins on the input via the spread.
    return (
      <div
        className={cn(
          "flex h-10 w-full items-center rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2",
          disabled && "cursor-not-allowed opacity-50",
          className,
        )}
      >
        <input
          type={type}
          ref={ref}
          disabled={disabled}
          className="w-full min-w-0 flex-1 border-0 bg-transparent px-0 pb-0.5 pt-0 text-sm text-inherit outline-none placeholder:text-muted-foreground disabled:cursor-not-allowed"
          style={{ WebkitAppearance: "none", appearance: "none", lineHeight: "1.5", ...style }}
          {...props}
        />
      </div>
    );
  },
);
Input.displayName = "Input";

export { Input };
