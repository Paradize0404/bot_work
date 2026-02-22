"""
Проверить статус последних CI-запусков на GitHub Actions.
Использование: python ci_status.py [--watch]
  --watch  : обновлять каждые 15 сек пока не завершится
"""

import sys
import time
import urllib.request
import json

REPO = "Paradize0404/bot_work"
URL = f"https://api.github.com/repos/{REPO}/actions/runs?per_page=5"

ICONS = {"success": "✅", "failure": "❌", "cancelled": "⚠️"}


def fetch():
    with urllib.request.urlopen(URL) as r:
        return json.loads(r.read())["workflow_runs"]


def print_runs(runs):
    print(
        f"\n{'SHA':>7}  {'Status':>12}  {'Conclusion':>12}  {'Triggered at':>22}  Message"
    )
    print("-" * 90)
    for run in runs:
        sha = run["head_sha"][:7]
        status = run["status"]
        conclusion = run.get("conclusion") or "—"
        icon = ICONS.get(conclusion, "⏳")
        created = run["created_at"]
        msg = run["display_title"][:50]
        print(f"{sha:>7}  {status:>12}  {icon} {conclusion:>10}  {created}  {msg}")


if __name__ == "__main__":
    watch = "--watch" in sys.argv

    if watch:
        print(f"Watching CI for {REPO} (Ctrl+C to stop)...")
        while True:
            try:
                runs = fetch()
                print_runs(runs)
                latest = runs[0]
                if latest["status"] == "completed":
                    print(f"\nDone: {latest.get('conclusion')}")
                    break
                time.sleep(15)
            except KeyboardInterrupt:
                break
    else:
        print_runs(fetch())
