// Server entry. Railway's start command runs `node dist/index.js`, which boots the Hono SSE server
// on $PORT. The CLI transport is a separate entry (cli.ts, `npm run chat`).

import { startServer } from "./server.js";

startServer();
