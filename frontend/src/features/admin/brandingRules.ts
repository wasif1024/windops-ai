// The checks the Tenant Service makes, so a mistake is shown before a round trip. It checks again.

export const HEX = /^#[0-9a-f]{6}$/;
export const LOGO_MAXIMUM_BYTES = 256 * 1024;
export const LOGO_TYPES = ["image/png", "image/jpeg"];

function luminance(hex: string): number {
  const channel = (start: number) => {
    const value = parseInt(hex.slice(start, start + 2), 16) / 255;
    return value <= 0.03928 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4;
  };
  return 0.2126 * channel(1) + 0.7152 * channel(3) + 0.0722 * channel(5);
}

/** WCAG contrast of white text on a color, which must be at least 4.5 for the primary color. */
export function contrastWithWhite(hex: string): number {
  return 1.05 / (luminance(hex) + 0.05);
}

/** The placeholders a text uses, such as user_name in {{user_name}}. */
export function placeholdersIn(text: string): string[] {
  return [...text.matchAll(/\{\{\s*([a-z_]+)\s*\}\}/g)].map((match) => match[1] ?? "");
}

export function logoProblem(file: File): string | null {
  if (!LOGO_TYPES.includes(file.type)) return "A logo is a PNG or JPEG. SVG is not allowed, as it can carry scripts.";
  if (file.size > LOGO_MAXIMUM_BYTES) return "A logo is at most 256 KiB.";
  return null;
}
