# I start from a slim Python base image. I only need Python + pip.
FROM python:3.12-slim

# I want predictable working paths.
WORKDIR /app

# I install the packages I need:
# - requests / flask for HTTP + API server
# - anything I need for talking to Plex/Lidarr/etc.
#
# NOTE:
# If you add more imports to run.py later, remember to add them here too.
RUN pip install --no-cache-dir \
    requests \
    flask

# I create a place to persist runtime state.
# docker-compose volume-mounts a host directory onto /app/config.
RUN mkdir -p /app/config

# I copy my brain (the script) into the container.
COPY run.py /app/run.py

# I expose 7000 because that's where my Flask API listens.
EXPOSE 7000

# This is the only process that needs to stay in the foreground.
# run.py itself:
#   - starts background threads (Plex polling / finished-track watcher)
#   - launches the Flask API server.
CMD ["python", "/app/run.py"]

