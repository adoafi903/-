#!/bin/bash
cd "$(dirname "$0")"
python3 -c "import PIL, pillow_heif" 2>/dev/null || { echo "初回だけ、画像を表示する部品を入れています…"; python3 -m pip install --user --quiet pillow pillow-heif; }
python3 app.py
