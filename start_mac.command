#!/bin/bash
cd "$(dirname "$0")"
if command -v python3 >/dev/null 2>&1; then
  python3 fukugen.py "$@"
else
  echo "Python 3 が見つかりません。ターミナルで xcode-select --install を実行してから、もう一度開いてください。"
fi
echo
read -n 1 -s -r -p "何かキーを押すと閉じます。"
