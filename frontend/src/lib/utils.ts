import { clsx, type ClassValue } from "clsx";
import { twMerge } from "tailwind-merge";

/** Canonical `cn` helper used by every shadcn/ui component. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
