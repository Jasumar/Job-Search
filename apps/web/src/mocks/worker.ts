import { setupWorker } from "msw/browser";
import { handlers } from "./handlers";

/**
 * Browser-side MSW worker: intercepts fetch in dev so the wizard runs against
 * the 3.8f contract before the backend half exists. Started lazily from
 * main.tsx only when VITE_ENABLE_MOCKS is on; the public/mockServiceWorker.js
 * file must exist (npx msw init public/ — already in the repo).
 */
export const worker = setupWorker(...handlers);