# Web app

React, TypeScript and Vite front end for the job search tool. It is a pure API
client: no database access and no business logic beyond view state.

```bash
npm ci --legacy-peer-deps   # the flag resolves a TypeScript peer conflict with openapi-typescript
npm run dev                 # Vite proxies /api to 127.0.0.1:8000; open the URL Vite prints
npm test                    # Vitest (jsdom), API mocked with MSW
npm run build
```

API types are generated from the committed `openapi.json` with
`openapi-typescript` into `src/api/schema.d.ts`. Setup of the whole system:
the [root README](../../README.md).
