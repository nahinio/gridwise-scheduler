---
title: GridWise Scheduler
emoji: ⚡
colorFrom: indigo
colorTo: green
sdk: docker
app_port: 7860
pinned: false
---

# GridWise Scheduler

LLM-assisted 24-hour campus energy scheduling API (BUP CSE Fest 2026 · GridWise).

- `GET /health` → `{"status":"ok"}`
- `POST /optimize-energy` → directive interpretation + 24-hour plan
- `GET /docs` → interactive API docs

Source, tests and full documentation: https://github.com/nahinio/gridwise-scheduler

The `OPENAI_API_KEY` is supplied as a Space **secret** (Settings → Variables and secrets);
it is never stored in this repository or in the image.
