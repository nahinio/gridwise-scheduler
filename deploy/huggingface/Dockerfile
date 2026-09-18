# Hugging Face Space (Docker SDK) for GridWise Scheduler.
# Runs the exact image that passed CI; nothing is rebuilt here.
# Note: since mid-2026 creating a Docker Space requires a paid Hugging Face plan (PRO/Team).
FROM ghcr.io/nahinio/gridwise-scheduler@sha256:c7f733c81c33b988cec6da3604aed4fb6a1ac0f4a20ef843132d9316651fbfcb

# Spaces run containers as uid 1000, so give that uid a real account and home directory.
USER root
RUN useradd --create-home --uid 1000 user
USER user

# Spaces route traffic to app_port (see README.md front matter). The app binds 0.0.0.0:$PORT.
ENV PORT=7860
EXPOSE 7860
