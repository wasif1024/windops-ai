import type { ButtonHTMLAttributes } from "react";

type Variant = "primary" | "outline";

const STYLES: Record<Variant, string> = {
  primary: "bg-brand-primary text-white hover:opacity-90",
  outline: "border border-current bg-transparent hover:bg-white/10",
};

/** The one button, so every screen looks and behaves the same. */
export function Button({
  variant = "primary",
  className = "",
  type = "button",
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { variant?: Variant }) {
  return (
    <button
      type={type}
      className={`cursor-pointer rounded-md px-4 py-2 font-medium ${STYLES[variant]} ${className}`}
      {...rest}
    />
  );
}
