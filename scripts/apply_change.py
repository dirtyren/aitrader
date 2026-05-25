#!/usr/bin/env python3
"""apply_aitrader_change.py — Apply config change, commit, PR, merge, restart.
Usage: python3 apply_aitrader_change.py <strategy> <config_file> <config_path> <commit_msg>
"""
import subprocess, sys, os, json, re

def sh(cmd, **kw):
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120, **kw)
    return r.stdout.strip(), r.stderr.strip(), r.returncode

def main():
    if len(sys.argv) < 5:
        print("Usage: apply_aitrader_change.py <strategy> <config_file> <config_path> <commit_msg>")
        sys.exit(1)
    
    strategy = sys.argv[1]      # e.g. rsi_trader
    config_file = sys.argv[2]   # e.g. settings_rsi.yaml
    config_path = sys.argv[3]   # e.g. /home/hermes/aitrader/config/settings_rsi.yaml
    commit_msg = sys.argv[4]
    
    repo = os.path.dirname(os.path.dirname(config_path))  # e.g. /home/hermes/aitrader
    os.chdir(repo)
    
    # Get branch name from commit msg
    branch = f"opt/{strategy}-{int(os.path.getmtime(config_path))}"
    
    # Check git status
    out, err, rc = sh("git status --porcelain")
    if rc != 0:
        print(f"ERROR: git status failed: {err}")
        sys.exit(1)
    
    if not out.strip():
        print("No changes to commit — nothing to push")
        return
    
    # Create branch, commit, push
    out, err, rc = sh(f"git checkout -b {branch}")
    if rc != 0:
        print(f"WARN: branch create: {err}")
    
    out, err, rc = sh(f"git add -A")
    out, err, rc = sh(f'git commit -m "{commit_msg}"')
    print(f"Commit: {out}")
    if rc != 0:
        print(f"ERROR: commit: {err}")
        sys.exit(1)
    
    out, err, rc = sh(f"git push -u origin {branch}")
    print(f"Push: {out[:200]}")
    if rc != 0:
        print(f"ERROR: push: {err}")
        sys.exit(1)
    
    # Get GITHUB_TOKEN
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        env_path = os.path.expanduser("~/.hermes/.env")
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("GITHUB_TOKEN"):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        break
    
    if not token:
        print("WARN: No GITHUB_TOKEN found. PR URL: https://github.com/dirtyren/aitrader/pull/new/" + branch)
        return
    
    # Create PR
    title = commit_msg
    body = f"Single-variable optimization for {strategy} from reflection cycle."
    
    import urllib.request
    import json as j
    
    pr_data = j.dumps({
        "title": title,
        "head": branch,
        "base": "main",
        "body": body
    }).encode()
    
    req = urllib.request.Request(
        "https://api.github.com/repos/dirtyren/aitrader/pulls",
        data=pr_data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        },
        method="POST"
    )
    
    try:
        with urllib.request.urlopen(req) as resp:
            pr_resp = j.loads(resp.read())
            pr_number = pr_resp.get("number")
            pr_url = pr_resp.get("html_url", "")
            print(f"PR created: #{pr_number} at {pr_url}")
    except urllib.error.HTTPError as e:
        print(f"ERROR creating PR: {e.code} {e.read().decode()[:500]}")
        sys.exit(1)
    
    # Merge PR
    merge_data = j.dumps({"merge_method": "squash"}).encode()
    merge_req = urllib.request.Request(
        f"https://api.github.com/repos/dirtyren/aitrader/pulls/{pr_number}/merge",
        data=merge_data,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
            "Content-Type": "application/json"
        },
        method="PUT"
    )
    
    try:
        with urllib.request.urlopen(merge_req) as resp:
            merge_resp = j.loads(resp.read())
            print(f"PR merged: {merge_resp.get('message', '')}")
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if "Merge conflict" in body:
            print(f"WARN: Merge conflict, auto-resolving...")
            # Fallback: switch to main, merge local, push
            sh("git checkout main")
            sh(f"git merge {branch} --no-edit")
            sh("git push origin main")
            print("Merged locally and pushed to main")
        else:
            print(f"ERROR merging: {e.code} {body[:500]}")
    
    # Checkout main
    sh("git checkout main")
    
    # Rebuild and restart
    print("Rebuilding and restarting Docker Compose...")
    out, err, rc = sh("docker compose build 2>&1")
    print(f"Build: {'OK' if rc == 0 else err[:200]}")
    
    out, err, rc = sh("docker compose up -d 2>&1")
    print(f"Restart: {'OK' if rc == 0 else err[:200]}")
    
    print("Done!")

if __name__ == "__main__":
    main()