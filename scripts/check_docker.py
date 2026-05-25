#!/usr/bin/env python3
"""Check unreflected trades per strategy in MySQL via docker exec with proper PYTHONPATH."""
import subprocess
import json

# The docker mysql client approach - let's try using a heredoc piped to docker exec
# or just use the mysql client installed on the host
result = subprocess.run(
    ["which", "mysql"],
    capture_output=True, text=True
)
print("mysql client:", result.stdout.strip() or "not found")

# Check if we have docker access
result = subprocess.run(
    ["docker", "ps", "--format", "{{.Names}}"],
    capture_output=True, text=True, timeout=5
)
print("docker stdout:", result.stdout)
print("docker stderr:", result.stderr)
print("docker rc:", result.returncode)
