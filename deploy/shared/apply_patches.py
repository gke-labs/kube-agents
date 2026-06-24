import pathlib

# Patch jobs.py
jobs_path = pathlib.Path("/opt/hermes/cron/jobs.py")
if jobs_path.exists():
    content = jobs_path.read_text(encoding="utf-8")
    
    # 1. Modify create_job signature to accept session_id
    old_sig = "workdir: Optional[str] = None,\n    no_agent: bool = False,"
    new_sig = "workdir: Optional[str] = None,\n    session_id: Optional[str] = None,\n    no_agent: bool = False,"
    if old_sig in content:
        content = content.replace(old_sig, new_sig, 1)
        print("  - Modified create_job signature")
    else:
        print("Error: create_job signature target not found in jobs.py")
        
    # 2. Add session_id to job dictionary
    old_dict = '"enabled_toolsets": normalized_toolsets,\n        "workdir": normalized_workdir,\n    }'
    new_dict = '"enabled_toolsets": normalized_toolsets,\n        "workdir": normalized_workdir,\n        "session_id": session_id,\n    }'
    if old_dict in content:
        content = content.replace(old_dict, new_dict, 1)
        print("  - Added session_id to job dict")
    else:
        # Retry with trailing comma if formatting is slightly different
        old_dict_alt = '"enabled_toolsets": normalized_toolsets,\n        "workdir": normalized_workdir,\n    }'
        print("Error: job dict target not found in jobs.py")
        
    jobs_path.write_text(content, encoding="utf-8")
    print("Successfully patched jobs.py")

# Patch scheduler.py
sched_path = pathlib.Path("/opt/hermes/cron/scheduler.py")
if sched_path.exists():
    content = sched_path.read_text(encoding="utf-8")
    
    # Modify _cron_session_id assignment
    old_assign = "_cron_session_id = f\"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}\""
    new_assign = "_cron_session_id = job.get(\"session_id\") or f\"cron_{job_id}_{_hermes_now().strftime('%Y%m%d_%H%M%S')}\""
    if old_assign in content:
        content = content.replace(old_assign, new_assign, 1)
        print("  - Modified _cron_session_id assignment")
    else:
        print("Error: _cron_session_id target not found in scheduler.py")
        
    sched_path.write_text(content, encoding="utf-8")
    print("Successfully patched scheduler.py")
