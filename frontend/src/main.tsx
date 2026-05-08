import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import App from "./App";
import { installAuthFetch } from "./lib/auth";

// Inject Bearer token into every /api/v1 request and force re-login on 401.
installAuthFetch();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);
