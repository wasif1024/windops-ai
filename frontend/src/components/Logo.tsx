import { useState } from "react";
import { type Branding, logoUrl } from "../api/client";

/** The tenant's logo, if it has one. Its alt text is empty, because the tenant's name is beside it. */
export function Logo({ branding }: { branding: Branding | null }) {
  const [failed, setFailed] = useState(false);
  if (!branding?.has_logo || failed) return null;
  return (
    <img
      className="max-h-10 max-w-40"
      src={logoUrl(branding.tenant_id)}
      alt=""
      onError={() => setFailed(true)}
    />
  );
}
