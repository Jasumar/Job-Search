/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** API base URL the frontend calls. Dev default: the vite /api proxy. */
  readonly VITE_API_BASE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
