import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./app/App";
import { AuthProvider } from "./auth/auth";
import "./styles/index.css";

const root = document.getElementById("root");
if (!root) throw new Error("The page has no root element");
createRoot(root).render(
  <StrictMode>
    <AuthProvider>
      <App />
    </AuthProvider>
  </StrictMode>,
);
