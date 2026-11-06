import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { AuthProvider } from "./auth/auth";
import { loadConfig } from "./config";
import "./styles/index.css";

const root = document.getElementById("root");
if (!root) throw new Error("The page has no root element");
const app = createRoot(root);

loadConfig()
  .then((config) =>
    app.render(
      <StrictMode>
        <AuthProvider config={config}>
          <App />
        </AuthProvider>
      </StrictMode>,
    ),
  )
  .catch(() =>
    app.render(<p className="p-8 text-slate-600">WindOps cannot start right now. Reload the page to try again.</p>),
  );
