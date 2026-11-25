import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./index.css";

// MSW browser worker (optional): intercepts fetch so the 3.8f wizard runs
// against the contract before the backend half exists. Off by default.
if (import.meta.env.VITE_ENABLE_MOCKS === "true") {
  const { worker } = await import("./mocks/worker");
  await worker.start({ onUnhandledRequest: "bypass" });
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
