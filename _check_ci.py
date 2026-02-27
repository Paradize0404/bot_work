import requests

r = requests.get(
    "https://api.github.com/repos/Paradize0404/bot_work/actions/runs",
    params={"per_page": 1},
)
run = r.json()["workflow_runs"][0]
sha = run["head_sha"][:7]
print(f"Commit: {sha}  Status: {run['conclusion'] or 'running'}")

jr = requests.get(
    f"https://api.github.com/repos/Paradize0404/bot_work/actions/runs/{run['id']}/jobs"
)
data = jr.json()
if "jobs" not in data:
    print("Rate limited or error:", jr.status_code, data.get("message", ""))
else:
    for j in data["jobs"]:
        for s in j["steps"]:
            c = s["conclusion"] or "?"
            marker = "FAIL" if c == "failure" else "  OK"
            print(f"  {marker}: {s['name']}")
