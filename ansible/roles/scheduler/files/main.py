#!/usr/bin/env python3
"""Lightweight cron-style scheduler with REST API.

Reads actions and schedule entries from a YAML config, executes HTTP calls
on cron schedules, and exposes a REST API for runtime management.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import yaml
from aiohttp import web
from croniter import croniter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("scheduler")

CONFIG_PATH = os.getenv("SCHEDULER_CONFIG", "/etc/scheduler/config.yml")
DB_PATH = os.getenv("SCHEDULER_DB", "/var/lib/scheduler/schedules.db")
PORT = int(os.getenv("SCHEDULER_PORT", "8100"))


# ── Database ─────────────────────────────────────────────────────────────────

def init_db(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schedules (
            id          TEXT PRIMARY KEY,
            cron        TEXT NOT NULL,
            action      TEXT NOT NULL,
            params      TEXT NOT NULL DEFAULT '{}',
            enabled     INTEGER NOT NULL DEFAULT 1,
            description TEXT NOT NULL DEFAULT '',
            last_run    TEXT,
            last_result TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS run_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            schedule_id TEXT NOT NULL,
            ran_at      TEXT NOT NULL,
            result      TEXT NOT NULL,
            detail      TEXT,
            FOREIGN KEY (schedule_id) REFERENCES schedules(id)
        )
    """)
    conn.commit()
    return conn


def seed_entries(conn: sqlite3.Connection, entries: list[dict]):
    """Insert seed entries only if they don't already exist."""
    for entry in entries:
        existing = conn.execute("SELECT id FROM schedules WHERE id = ?", (entry["id"],)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO schedules (id, cron, action, params, enabled, description) VALUES (?, ?, ?, ?, ?, ?)",
                (entry["id"], entry["cron"], entry["action"],
                 json.dumps(entry.get("params", {})),
                 1 if entry.get("enabled", True) else 0,
                 entry.get("description", "")),
            )
            log.info("Seeded schedule: %s", entry["id"])
    conn.commit()


# ── Config ───────────────────────────────────────────────────────────────────

def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# ── Executor ─────────────────────────────────────────────────────────────────

async def execute_action(actions: dict, action_name: str, params: dict) -> tuple[str, str]:
    """Execute an action by making an HTTP call. Returns (result, detail)."""
    action = actions.get(action_name)
    if not action:
        return "error", f"Unknown action: {action_name}"

    url = action["endpoint"]
    method = action.get("method", "POST").upper()

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if method == "POST":
                async with session.post(url, json=params) as resp:
                    body = await resp.text()
                    if resp.status < 400:
                        return "ok", f"{resp.status}: {body[:200]}"
                    return "error", f"{resp.status}: {body[:200]}"
            elif method == "GET":
                async with session.get(url, params={k: str(v) for k, v in params.items()}) as resp:
                    body = await resp.text()
                    if resp.status < 400:
                        return "ok", f"{resp.status}: {body[:200]}"
                    return "error", f"{resp.status}: {body[:200]}"
            elif method == "PUT":
                async with session.put(url, json=params) as resp:
                    body = await resp.text()
                    if resp.status < 400:
                        return "ok", f"{resp.status}: {body[:200]}"
                    return "error", f"{resp.status}: {body[:200]}"
            else:
                return "error", f"Unsupported method: {method}"
    except Exception as e:
        return "error", str(e)


# ── Scheduler Loop ───────────────────────────────────────────────────────────

async def scheduler_loop(conn: sqlite3.Connection, actions: dict):
    """Main loop: check every 30s for schedules that need to run."""
    log.info("Scheduler loop started with %d actions", len(actions))

    while True:
        now = datetime.now()
        rows = conn.execute(
            "SELECT * FROM schedules WHERE enabled = 1"
        ).fetchall()

        for row in rows:
            schedule_id = row["id"]
            cron_expr = row["cron"]
            last_run_str = row["last_run"]

            # Determine if this schedule should fire
            cron = croniter(cron_expr, now)
            prev_fire = cron.get_prev(datetime)

            if last_run_str:
                last_run = datetime.fromisoformat(last_run_str)
                if last_run >= prev_fire:
                    continue  # Already ran for this window

            # Fire it
            params = json.loads(row["params"])
            log.info("Running schedule: %s (action: %s)", schedule_id, row["action"])
            result, detail = await execute_action(actions, row["action"], params)

            now_iso = datetime.now().isoformat()
            conn.execute(
                "UPDATE schedules SET last_run = ?, last_result = ? WHERE id = ?",
                (now_iso, result, schedule_id),
            )
            conn.execute(
                "INSERT INTO run_log (schedule_id, ran_at, result, detail) VALUES (?, ?, ?, ?)",
                (schedule_id, now_iso, result, detail),
            )
            conn.commit()
            log.info("Schedule %s result: %s — %s", schedule_id, result, detail[:100])

        await asyncio.sleep(30)


# ── REST API ─────────────────────────────────────────────────────────────────

def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["params"] = json.loads(d["params"])
    d["enabled"] = bool(d["enabled"])
    return d


def make_app(conn: sqlite3.Connection, actions: dict) -> web.Application:
    routes = web.RouteTableDef()

    @routes.get("/schedules")
    async def list_schedules(request):
        rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        return web.json_response([row_to_dict(r) for r in rows])

    @routes.post("/schedules")
    async def create_schedule(request):
        data = await request.json()
        required = {"id", "cron", "action"}
        if not required.issubset(data):
            return web.json_response({"error": f"Missing fields: {required - set(data)}"}, status=400)
        if not croniter.is_valid(data["cron"]):
            return web.json_response({"error": f"Invalid cron expression: {data['cron']}"}, status=400)
        if data["action"] not in actions:
            return web.json_response({"error": f"Unknown action: {data['action']}. Available: {list(actions)}"}, status=400)
        try:
            conn.execute(
                "INSERT INTO schedules (id, cron, action, params, enabled, description) VALUES (?, ?, ?, ?, ?, ?)",
                (data["id"], data["cron"], data["action"],
                 json.dumps(data.get("params", {})),
                 1 if data.get("enabled", True) else 0,
                 data.get("description", "")),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            return web.json_response({"error": f"Schedule '{data['id']}' already exists"}, status=409)
        return web.json_response({"created": data["id"]}, status=201)

    @routes.get("/schedules/status")
    async def schedules_status(request):
        rows = conn.execute("SELECT * FROM schedules ORDER BY id").fetchall()
        status = []
        now = datetime.now()
        for row in rows:
            entry = {"id": row["id"], "enabled": bool(row["enabled"]),
                     "last_run": row["last_run"], "last_result": row["last_result"]}
            if croniter.is_valid(row["cron"]):
                entry["next_run"] = croniter(row["cron"], now).get_next(datetime).isoformat()
            status.append(entry)
        return web.json_response({"schedules": status})

    @routes.get("/schedules/{id}")
    async def get_schedule(request):
        schedule_id = request.match_info["id"]
        row = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        if not row:
            return web.json_response({"error": "Not found"}, status=404)
        d = row_to_dict(row)
        # Include recent runs
        runs = conn.execute(
            "SELECT ran_at, result, detail FROM run_log WHERE schedule_id = ? ORDER BY ran_at DESC LIMIT 10",
            (schedule_id,),
        ).fetchall()
        d["recent_runs"] = [dict(r) for r in runs]
        return web.json_response(d)

    @routes.put("/schedules/{id}")
    async def update_schedule(request):
        schedule_id = request.match_info["id"]
        existing = conn.execute("SELECT * FROM schedules WHERE id = ?", (schedule_id,)).fetchone()
        if not existing:
            return web.json_response({"error": "Not found"}, status=404)
        data = await request.json()

        cron = data.get("cron", existing["cron"])
        if not croniter.is_valid(cron):
            return web.json_response({"error": f"Invalid cron expression: {cron}"}, status=400)
        action = data.get("action", existing["action"])
        if action not in actions:
            return web.json_response({"error": f"Unknown action: {action}"}, status=400)

        conn.execute(
            "UPDATE schedules SET cron = ?, action = ?, params = ?, enabled = ?, description = ? WHERE id = ?",
            (cron, action,
             json.dumps(data["params"]) if "params" in data else existing["params"],
             1 if data.get("enabled", bool(existing["enabled"])) else 0,
             data.get("description", existing["description"]),
             schedule_id),
        )
        conn.commit()
        return web.json_response({"updated": schedule_id})

    @routes.delete("/schedules/{id}")
    async def delete_schedule(request):
        schedule_id = request.match_info["id"]
        result = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        if result.rowcount == 0:
            return web.json_response({"error": "Not found"}, status=404)
        conn.execute("DELETE FROM run_log WHERE schedule_id = ?", (schedule_id,))
        conn.commit()
        return web.json_response({"deleted": schedule_id})

    @routes.get("/schedules/{id}/log")
    async def get_log(request):
        schedule_id = request.match_info["id"]
        limit = int(request.query.get("limit", "25"))
        runs = conn.execute(
            "SELECT ran_at, result, detail FROM run_log WHERE schedule_id = ? ORDER BY ran_at DESC LIMIT ?",
            (schedule_id, limit),
        ).fetchall()
        return web.json_response([dict(r) for r in runs])

    @routes.get("/actions")
    async def list_actions(request):
        return web.json_response(actions)

    @routes.get("/health")
    async def health(request):
        count = conn.execute("SELECT COUNT(*) FROM schedules WHERE enabled = 1").fetchone()[0]
        return web.json_response({"status": "ok", "active_schedules": count})

    app = web.Application()
    app.add_routes(routes)
    return app


# ── Main ─────────────────────────────────────────────────────────────────────

async def main():
    config = load_config(CONFIG_PATH)
    actions = {a["name"]: a for a in config.get("actions", [])}
    entries = config.get("schedules", [])

    conn = init_db(DB_PATH)
    seed_entries(conn, entries)

    app = make_app(conn, actions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("Scheduler API listening on port %d", PORT)

    await scheduler_loop(conn, actions)


if __name__ == "__main__":
    asyncio.run(main())
