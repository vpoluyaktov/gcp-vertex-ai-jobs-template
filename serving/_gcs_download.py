#!/usr/bin/env python3
"""Download a GCS prefix to a local directory.

Usage:
    python _gcs_download.py gs://bucket/prefix /local/dest

Used exclusively by the Dockerfile model-prep stage to optionally bake a LoRA
adapter into the image at build time.  Cloud Build Application Default
Credentials (ADC) handle authentication — no service-account key file needed.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <gs://bucket/prefix> <local-dest>", file=sys.stderr)
        sys.exit(1)

    gcs_uri = sys.argv[1]
    local_dir = sys.argv[2]

    if not gcs_uri.startswith("gs://"):
        print(f"ERROR: expected a gs:// URI, got: {gcs_uri!r}", file=sys.stderr)
        sys.exit(1)

    from google.cloud import storage  # noqa: PLC0415

    without_scheme = gcs_uri[len("gs://"):]
    bucket_name, _, prefix = without_scheme.partition("/")
    prefix = prefix.rstrip("/") + "/"

    client = storage.Client()
    blobs = list(client.list_blobs(bucket_name, prefix=prefix))

    if not blobs:
        print(f"ERROR: no objects found under {gcs_uri}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(local_dir, exist_ok=True)
    downloaded = 0
    for blob in blobs:
        relative = blob.name[len(prefix):]
        if not relative:
            # Skip the directory placeholder blob itself
            continue
        dest = os.path.join(local_dir, relative)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        blob.download_to_filename(dest)
        print(f"  ↓ {blob.name}  →  {dest}")
        downloaded += 1

    print(f"Downloaded {downloaded} object(s) from {gcs_uri} to {local_dir}")


if __name__ == "__main__":
    main()
