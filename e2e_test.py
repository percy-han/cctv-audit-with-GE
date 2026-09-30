import urllib.request
import json
import os
import subprocess
import time
import sys

def get_id_token():
    return subprocess.check_output(["gcloud", "auth", "print-identity-token"]).decode("utf-8").strip()

def _required_env(name, hint):
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"{name} is not set ({hint})")
    return value


# Every deployment-specific value comes from the environment (nothing names a real project/user):
#   CLOUD_RUN_WORKER_URL  main.tf output cloud_run_worker_pool_url, e.g. https://<name_prefix>-worker-<project number>.<region>.run.app
#   E2E_USER_ID           the supervisor email whose job is resumed
#   E2E_JOB_ID            an existing job of that user (resumed from its GCS SegmentCheckpoint)
url = _required_env("CLOUD_RUN_WORKER_URL", "main.tf output cloud_run_worker_pool_url").rstrip("/") + "/api/reasoning_engine"
token = get_id_token()
user_id = _required_env("E2E_USER_ID", "supervisor email that owns the job")
job_id = _required_env("E2E_JOB_ID", "existing job ID to resume")

def rpc_call(method, **kwargs):
    req_data = json.dumps({"class_method": method, "input": kwargs})
    print(f"Calling {method} with params {kwargs}...")
    req = urllib.request.Request(url, data=req_data.encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req)
        body = json.loads(resp.read())
        out = body.get("output", {})
        print(
            f"Response [{method}]: job_id={out.get('job_id')} state={out.get('state')} "
            f"model={out.get('active_model_version')} prompt={out.get('active_prompt_version')}",
            flush=True,
        )
        if "error" in out:
            raise Exception(f"RPC Error: {out['error']}")
        return out
    except urllib.error.HTTPError as e:
        print("HTTP Error:", e.code, e.read().decode())
        sys.exit(1)

# Resume or monitor the existing job E2E_JOB_ID from its GCS SegmentCheckpoint

# Step 2: Start / Resume Audit from checkpoint
rpc_call("start_audit", user_id=user_id, job_id=job_id)

# Step 3: Poll get_status
while True:
    time.sleep(10)
    req_data = json.dumps({"class_method": "get_status", "input": {"user_id": user_id, "job_id": job_id, "session_id": "test-session-9"}})
    req = urllib.request.Request(url, data=req_data.encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {get_id_token()}")
    req.add_header("Content-Type", "application/json")
    body = json.loads(urllib.request.urlopen(req).read())
    st = body.get("output", {})
    state = st.get("state", "running")
    done_segs = len(st.get("completed_segments") or {})
    total_segs = (st.get("preflight_report") or {}).get("planned_segments_count", 8)
    print(
        f"[Poll] job={job_id} state={state} model={st.get('active_model_version')} "
        f"segments={done_segs}/{total_segs} tokens={st.get('total_tokens_used', 0)} "
        f"violations={st.get('violations_found', 0)} sheet={st.get('report_sheet_url')} "
        f"err={st.get('error_message')}",
        flush=True,
    )
    if state in ("done", "failed", "error"):
        print("Final state reached:", json.dumps(st, indent=2, ensure_ascii=False), flush=True)
        break

print("E2E Test completed.")
