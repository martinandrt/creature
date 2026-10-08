# The workshop: where skills render files. No network at run time, so everything is baked in here.
# Base pinned by digest, Python packages pinned exactly; the built image ID is part of the fingerprint.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-inter
RUN pip install --no-cache-dir "pillow==11.3.0" "numpy==2.5.3"
