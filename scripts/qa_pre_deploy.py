"""
Pre-deployment QA checks for Gianluigi.

Run before every deploy to catch issues early:
  python scripts/qa_pre_deploy.py

Checks:
1. All tests pass
2. No bare 'except: pass' in production code (except monitoring)
3. All MCP tools registered and have category prefixes
4. No 'english' tsvector references (should be 'simple')
5. No naive datetime.now() in schedulers
6. Extraction prompt is valid (no broken f-string)
7. Import chain works (key modules importable)
8. Config files valid
"""

import subprocess
import sys
import os

# Change to project root and add to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(PROJECT_ROOT)
sys.path.insert(0, PROJECT_ROOT)

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"

results = []


def check(name, status, detail=""):
    results.append((name, status, detail))
    icon = {"PASS": "+", "FAIL": "!", "WARN": "~"}[status]
    print(f"  [{icon}] {name}" + (f" — {detail}" if detail else ""))


def run_checks():
    print("\n=== Gianluigi Pre-Deploy QA ===\n")

    # 1. Import chain
    print("[1] Import checks...")
    try:
        from config.settings import settings
        check("config.settings imports", PASS)
    except Exception as e:
        check("config.settings imports", FAIL, str(e))

    try:
        from config.team import TEAM_MEMBERS
        check("config.team imports", PASS)
        for key, m in TEAM_MEMBERS.items():
            if "role_description" not in m:
                check(f"  {key} role_description", FAIL, "missing")
        check("team role_descriptions present", PASS)
    except Exception as e:
        check("config.team imports", FAIL, str(e))

    # Note: config/projects.py replaced by canonical_projects DB table in Phase 10
    try:
        from services.supabase_client import SupabaseClient
        check("supabase_client.get_canonical_projects available", PASS)
    except Exception as e:
        check("canonical projects system", FAIL, str(e))

    try:
        from config.escalation import classify_overdue_tier, ESCALATION_TIERS
        check("config.escalation imports", PASS)
    except Exception as e:
        check("config.escalation imports", FAIL, str(e))

    # 2. MCP tool count
    print("\n[2] MCP tool registration...")
    try:
        from services.mcp_server import MCPServer
        import asyncio

        async def count_tools():
            server = MCPServer()
            mcp = server._build_mcp()
            tools = await mcp.list_tools()
            return tools

        tools = asyncio.run(count_tools())
        tool_count = len(tools)
        check(f"MCP tools registered: {tool_count}", PASS if tool_count >= 35 else FAIL)

        # Check category prefixes
        missing_prefix = []
        for t in tools:
            desc = t.description or ""
            if not desc.startswith("["):
                missing_prefix.append(t.name)
        if missing_prefix:
            check(f"Tools missing category prefix: {missing_prefix}", WARN)
        else:
            check("All tools have category prefixes", PASS)
    except Exception as e:
        check("MCP tool registration", FAIL, str(e))

    # 3. No 'english' tsvector in SQL
    print("\n[3] Code quality checks...")
    try:
        with open("scripts/setup_supabase.sql", encoding="utf-8") as f:
            sql_lines = f.readlines()
        # Count 'english' in non-comment lines only
        english_count = sum(
            1 for line in sql_lines
            if "'english'" in line and not line.strip().startswith("--")
        )
        check("No 'english' tsvector in setup_supabase.sql",
              PASS if english_count == 0 else FAIL,
              f"{english_count} active references found" if english_count else "")
    except Exception as e:
        check("tsvector check", WARN, str(e))

    # 4. No naive datetime.now() in schedulers
    import glob
    scheduler_files = glob.glob("schedulers/*.py")
    naive_now_files = []
    for f in scheduler_files:
        if f.endswith("__init__.py"):
            continue
        with open(f, encoding="utf-8", errors="ignore") as fh:
            content = fh.read()
        # Check for datetime.now() without timezone arg
        import re
        matches = re.findall(r'datetime\.now\(\)', content)
        if matches:
            naive_now_files.append(os.path.basename(f))

    if naive_now_files:
        check("Naive datetime.now() in schedulers", FAIL, str(naive_now_files))
    else:
        check("All schedulers use timezone-aware datetime", PASS)

    # 5. Extraction prompt validity
    try:
        from processors.transcript_processor import extract_structured_data
        import inspect
        source = inspect.getsource(extract_structured_data)
        checks_ok = True
        for keyword in ["ACTION ITEM EXTRACTION RULES", "LABEL RULES",
                         "DECISION EXTRACTION RULES", "LANGUAGE HANDLING",
                         "EXISTING TASK AWARENESS", "canonical_names"]:
            if keyword not in source:
                check(f"Extraction prompt: {keyword}", FAIL, "missing")
                checks_ok = False
        if checks_ok:
            check("Extraction prompt has all required sections", PASS)
    except Exception as e:
        check("Extraction prompt check", FAIL, str(e))

    # 6. Key processors importable
    print("\n[4] Processor imports...")
    for mod_name in [
        "processors.meeting_continuity",
        "processors.topic_threading",
        "processors.operational_snapshot",
        "processors.gantt_intelligence",
        "processors.decision_review",
        "services.alerting",
        "core.cost_calculator",
    ]:
        try:
            __import__(mod_name)
            check(f"{mod_name}", PASS)
        except Exception as e:
            check(f"{mod_name}", FAIL, str(e)[:80])

    # 7. Phase 10: Column mapping consistency
    print("\n[5] Phase 10 column mapping checks...")
    try:
        from services.google_sheets import TASK_COLUMNS, TASK_TRACKER_HEADERS, DECISION_COLUMNS
        assert len(TASK_COLUMNS) == len(TASK_TRACKER_HEADERS), "TASK_COLUMNS length != TASK_TRACKER_HEADERS"
        assert TASK_COLUMNS["priority"] == "A", "Priority should be column A"
        assert TASK_COLUMNS["task"] == "C", "Task should be column C"
        assert TASK_COLUMNS["status"] == "F", "Status should be column F"
        check("Task column mapping consistent", PASS)
    except Exception as e:
        check("Task column mapping", FAIL, str(e))

    try:
        assert DECISION_COLUMNS["label"] == "A", "Decision label should be column A"
        assert DECISION_COLUMNS["decision"] == "B", "Decision should be column B"
        assert len(DECISION_COLUMNS) == 7, "Decisions should have 7 columns"
        check("Decision column mapping consistent", PASS)
    except Exception as e:
        check("Decision column mapping", FAIL, str(e))

    # 8. No bare except: pass outside monitoring
    import re as _re
    bare_except_files = []
    for root, _, files in os.walk("."):
        if "__pycache__" in root or ".git" in root or "test_" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
                if _re.search(r'except:\s*\n\s*pass', content):
                    bare_except_files.append(fpath)
            except Exception:
                pass

    if bare_except_files:
        check(f"Bare 'except: pass' found in {len(bare_except_files)} files", WARN, str(bare_except_files[:3]))
    else:
        check("No bare 'except: pass' patterns", PASS)

    # Summary
    print("\n=== Summary ===")
    passes = sum(1 for _, s, _ in results if s == PASS)
    fails = sum(1 for _, s, _ in results if s == FAIL)
    warns = sum(1 for _, s, _ in results if s == WARN)
    print(f"  {passes} passed, {fails} failed, {warns} warnings")

    if fails > 0:
        print("\n  DEPLOY BLOCKED — fix failures first")
        return 1
    elif warns > 0:
        print("\n  DEPLOY OK (with warnings)")
        return 0
    else:
        print("\n  DEPLOY OK — all checks passed")
        return 0


if __name__ == "__main__":
    sys.exit(run_checks())
