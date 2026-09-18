# Hugging Face Space (Docker SDK) for GridWise Scheduler.
# Runs the exact image that passed CI; nothing is rebuilt here.
FROM ghcr.io/nahinio/gridwise-scheduler@sha256:c0df46e03d054fe22f2086fc4186bb4b891d864aeec15ed2490f7618a0c512d1

# Spaces run containers as uid 1000, so give that uid a real account and home directory.
USER root
RUN useradd --create-home --uid 1000 user
USER user

# Spaces route traffic to app_port (see README.md front matter). The app binds 0.0.0.0:$PORT.
ENV PORT=7860
EXPOSE 7860
