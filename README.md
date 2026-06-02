# FinAlly — AI Trading Workstation

A Bloomberg-inspired AI trading workstation that streams live market data, runs a simulated portfolio, and lets an LLM chat assistant analyze positions and execute trades on the user's behalf.

Built entirely by coding agents as a capstone project for an agentic AI coding course. Agents coordinate through specs in `planning/`.

## Status

In active development.

- **Backend / market data** — complete. GBM simulator and Polygon.io (Massive) client behind a shared interface, in-memory price cache, SSE stream endpoint. See [`planning/MARKET_DATA_SUMMARY.md`](planning/MARKET_DATA_SUMMARY.md).
- **Portfolio, chat, frontend, Docker packaging** — not yet built.

## Vision

- Live price streaming via SSE with green/red flash animations
- Simulated portfolio — $10k virtual cash, market orders, instant fill
- Portfolio visualizations — treemap heatmap, P&L chart, positions table
- AI chat assistant — analyzes holdings, suggests and auto-executes trades and watchlist changes
- Single Docker container serving Next.js static export + FastAPI on port 8000
- SQLite with lazy initialization; no signup, no login

Full specification: [`planning/PLAN.md`](planning/PLAN.md).

## Tech Stack

- **Backend** — FastAPI, Python 3.12, managed with `uv`
- **Frontend** (planned) — Next.js + TypeScript + Tailwind, exported as static files
- **Database** — SQLite, volume-mounted
- **Real-time** — Server-Sent Events
- **AI** — LiteLLM → OpenRouter, `openrouter/openai/gpt-oss-120b` on Cerebras, structured outputs
- **Market data** — built-in GBM simulator (default) or Massive / Polygon.io (with `MASSIVE_API_KEY`)

## Running the Backend

```bash
cd backend
uv sync
uv run python market_data_demo.py     # rich terminal demo of the live price stream
uv run pytest                          # backend tests
```

See [`backend/README.md`](backend/README.md) for details.

## Environment

Copy `.env.example` to `.env` at the project root:

| Variable | Required | Description |
|---|---|---|
| `OPENROUTER_API_KEY` | Yes (for chat) | OpenRouter API key |
| `MASSIVE_API_KEY` | No | Polygon.io key for real market data; omit to use the simulator |
| `LLM_MOCK` | No | `true` for deterministic mock LLM responses (testing) |
| `CHAT_HISTORY_LIMIT` | No | Rows of chat history passed to the LLM (default 20) |
| `LLM_TIMEOUT_SECONDS` | No | Per-call LLM timeout (default 30) |

## Repository Layout

```
finally/
├── backend/    # FastAPI uv project (market data complete)
├── planning/   # Specs and agent contracts — start with PLAN.md
└── README.md
```

`frontend/`, `test/`, `scripts/`, `db/`, and the `Dockerfile` will be added as those components are built.

## License

See [LICENSE](LICENSE).
