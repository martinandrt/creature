# The workshop: where skills render files. No network at run time, so everything is baked in here.
# Base pinned by digest, Python packages pinned exactly, downloaded fonts pinned by checksum; the built
# image ID is part of the fingerprint.
FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-inter fonts-jetbrains-mono
RUN pip install --no-cache-dir "pillow==11.3.0" "numpy==2.5.3" "scipy==1.18.1"
# Barlow (SIL Open Font License) from the Google Fonts repository
ADD --checksum=sha256:95aa02c7c43096e0dd44d787ba6216864a67157e402adab59b35572e0c1577ea https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-Regular.ttf /usr/share/fonts/truetype/barlow/
ADD --checksum=sha256:f8906f762cb73dca441da034bc363b2d8e2e68bc10d5c05e58717646c20cc4b4 https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-Medium.ttf /usr/share/fonts/truetype/barlow/
ADD --checksum=sha256:86577cb32f8abe3673db53ca0f4221e6856751a4f6730c867e00f720f8bb1fc5 https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-SemiBold.ttf /usr/share/fonts/truetype/barlow/
ADD --checksum=sha256:84e6a4d61e7c3e21f3c50ea6a4f7e5303a3467864c038be6ea3759bab8d547f9 https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-Bold.ttf /usr/share/fonts/truetype/barlow/
ADD --checksum=sha256:5234d91030e1bb89525cbaccc4386892c3fe005a0582c705264ce3572d96a1a1 https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-ExtraBold.ttf /usr/share/fonts/truetype/barlow/
ADD --checksum=sha256:fc4f74e8475b181003f7597450c62d630622b417e367a6842972b286c49f9440 https://raw.githubusercontent.com/google/fonts/main/ofl/barlow/Barlow-Black.ttf /usr/share/fonts/truetype/barlow/
RUN chmod 644 /usr/share/fonts/truetype/barlow/*.ttf
