# Frontend (React + TypeScript, Vite)

Customer journey (interview with explicit "I don't know", insurer questions in their own wording, coverage comparison with clause citations, hash-bound approval review, issuance timeline with separate quoted / submitted / bound / issued / effective labels, agent chat) and an operator view (adapter requests, state transitions, pending actions, jobs, redacted evidence, metrics, mock controls).

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173, proxies /v1 to http://localhost:8000
npm run build        # tsc --noEmit && vite build
```

The dev server proxies `/v1` to `VITE_PROXY_TARGET` (default `http://127.0.0.1:8000`). For a built bundle, set `VITE_API_BASE` to the API origin at build time.

Sessions are selected from the top bar using the fixture tokens (three synthetic customers and one operator).
