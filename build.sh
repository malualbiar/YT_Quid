#!/usr/bin/env bash
# build.sh — Render build script for YT Quid (Django)
set -e

echo "==> Installing Python dependencies"
pip install -r requirements.txt

echo "==> Collecting static files"
python manage.py collectstatic --noinput

echo "==> Running database migrations"
python manage.py migrate --noinput

echo "==> Seeding demo data (skipped if already seeded)"
python manage.py seed_demo_data || true

echo "==> Build complete"
