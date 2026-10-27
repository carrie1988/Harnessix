#!/bin/bash
set -u
B="/Users/zhangjinhui/Library/Application Support/Harnessix/swebench"; W="$B/cap-counterfactual"
"$B/build-missing-images.sh" "$W/cases.json" "$W/build" || { echo "IMAGES_FAILED"; exit 2; }
for cap in 1000000 1500000; do
  PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH" python3 "$W/evaluate.py" $cap > "$W/evaluate-$cap.log" 2>&1
  echo "CAP_DONE $cap exit=$?"; tail -1 "$W/evaluate-$cap.log" | cut -c1-400
done
