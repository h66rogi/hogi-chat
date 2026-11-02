import json, os, sys, time, traceback
import duckdb

C = CREDS
EP, BK = C["endpoint"], C["bucket"]
SELF_PREFIX = os.environ.get("HOGI_SELF_ID", "self")
BJ = os.environ.get("HOGI_BJ_ID", "")
BJ_NAME = os.environ.get("HOGI_BJ_NAME", "")
PFX = os.environ.get("HOGI_ARCHIVE_PREFIX", "")
GLOB = "s3://" + BK + "/" + PFX + "year=*/month=*/day=*/hour=*/data.parquet"
BASE = os.path.expanduser("~/hogi-chat/archive")
TMP = os.path.join(BASE, "tmp")
OUT = os.path.join(BASE, "analysis_result.json")
os.makedirs(TMP, exist_ok=True)
t0 = time.time()
SECRETS = [C["ak"], C["sk"]]


def redact(s):
    s = str(s)
    for x in SECRETS:
        if x:
            s = s.replace(x, "<redacted>")
    return s[:300]


def log(k, v):
    print(json.dumps({k: v}, ensure_ascii=False), flush=True)


def mem():
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            k, _, v = line.partition(":")
            if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                info[k] = int(v.split()[0]) // 1024
    return info


res = {"glob": GLOB, "bucket": BK, "host": os.uname().nodename, "meminfo_mb": mem()}
avail = res["meminfo_mb"].get("MemAvailable", 0)
limit = max(1024, min(3072, int(avail * 0.5))) if avail else 2048
res["duckdb_memory_limit_mb"] = limit
log("start", {"available_mb": avail, "limit_mb": limit, "threads": 2})

c = duckdb.connect(config={"memory_limit": "%dMB" % limit, "threads": 2, "temp_directory": TMP,
                           "max_temp_directory_size": "8GB", "preserve_insertion_order": "false"})
c.execute("LOAD httpfs")
# TEMPORARY SECRET: session-only; DuckDB does not persist it to disk.
c.execute("CREATE OR REPLACE TEMPORARY SECRET r2 (TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION 'auto', URL_STYLE 'path')",
          [C["ak"], C["sk"], EP.replace("https://", "")])


def step(name, fn):
    t = time.time()
    try:
        res[name] = fn()
        log("ok", {"step": name, "secs": round(time.time() - t)})
    except Exception:
        res[name] = {"error": redact(traceback.format_exc().splitlines()[-1])}
        log("fail", {"step": name, "err": res[name]["error"], "secs": round(time.time() - t)})
    try:
        with open(OUT, "w") as fh:
            json.dump(res, fh, ensure_ascii=False)
    except Exception:
        log("fail", {"step": "write_result", "err": "unwritable"})


def hour_of(p):
    d = {}
    for part in p.split("/"):
        k, _, v = part.partition("=")
        d[k] = v
    return "%s-%s-%sT%s" % (d.get("year"), d.get("month"), d.get("day"), d.get("hour"))


def s_files():
    r = c.execute("SELECT file_name, num_rows, total_byte_size FROM parquet_file_metadata(?)", [GLOB]).fetchall()
    files = sorted((x[0], int(x[1] or 0), int(x[2] or 0)) for x in r)
    hours = sorted(hour_of(p) for p, _, _ in files)
    return {"files": len(files), "rows_meta": sum(n for _, n, _ in files),
            "bytes_meta": sum(b for _, _, b in files),
            "first_hour": hours[0] if hours else None, "last_hour": hours[-1] if hours else None,
            "distinct_hours": len(set(hours))}


def s_target():
    r = c.execute(f"""SELECT platform, channel_id, streamer_name, count(*) n, min(timestamp) t0, max(timestamp) t1,
                     count(DISTINCT user_id) users FROM read_parquet(?)
                     WHERE channel_id='{BJ}' OR streamer_name LIKE '%{BJ_NAME}%' GROUP BY 1,2,3 ORDER BY n DESC""",
                  [GLOB]).fetchall()
    hogi = [{"platform": x[0], "channel_id": x[1], "streamer_name": x[2], "n": x[3],
             "t0": str(x[4]), "t1": str(x[5]), "users": x[6]} for x in r]
    r2 = c.execute("""SELECT channel_id, streamer_name, platform, count(*) n, min(timestamp) t0, max(timestamp) t1
                      FROM read_parquet(?) WHERE lower(user_id) LIKE '%' || ?
                      GROUP BY 1,2,3 ORDER BY n DESC LIMIT 40""", [GLOB, SELF_PREFIX]).fetchall()
    sej = [{"channel_id": x[0], "streamer_name": x[1], "platform": x[2], "n": x[3],
            "t0": str(x[4]), "t1": str(x[5])} for x in r2]
    return {"hogi": hogi, "self": sej}


def s_hogi_hourly():
    r = c.execute(f"""SELECT date_trunc('hour', timestamp) h, count(*) n FROM read_parquet(?)
                     WHERE channel_id='{BJ}' OR streamer_name LIKE '%{BJ_NAME}%' GROUP BY 1 ORDER BY 1""",
                  [GLOB]).fetchall()
    return [{"hour": str(x[0]), "n": x[1]} for x in r][:600]


def s_platform_type():
    r = c.execute("""SELECT platform, message_type, count(*) n, count(DISTINCT channel_id) ch,
                     count(DISTINCT streamer_name) st FROM read_parquet(?) GROUP BY 1,2 ORDER BY n DESC""",
                  [GLOB]).fetchall()
    r2 = c.execute("""SELECT sum(CASE WHEN message_type='donation' THEN amount_krw ELSE 0 END) krw,
                      count(*) FILTER (WHERE message_type='donation') don FROM read_parquet(?)""", [GLOB]).fetchall()
    per_platform = {}
    total = 0
    for p, t, n, ch, st in r:
        per_platform.setdefault(p, {"n": 0, "types": {}, "channels_max": 0, "streamers_max": 0})
        per_platform[p]["n"] += n
        per_platform[p]["types"][t] = n
        per_platform[p]["channels_max"] = max(per_platform[p]["channels_max"], ch)
        per_platform[p]["streamers_max"] = max(per_platform[p]["streamers_max"], st)
        total += n
    return {"per_platform": per_platform, "total_rows": total,
            "donation": {"rows": r2[0][1], "amount_krw": r2[0][0]}}


def s_daily_hourly():
    r = c.execute("""SELECT CAST(timestamp AS DATE) d, platform, count(*) n, count(DISTINCT channel_id) ch
                     FROM read_parquet(?) GROUP BY 1,2 ORDER BY 1""", [GLOB]).fetchall()
    daily = [{"date": str(x[0]), "platform": x[1], "n": x[2], "channels": x[3]} for x in r]
    r2 = c.execute("""SELECT date_trunc('hour', timestamp) h, platform, count(*) n
                      FROM read_parquet(?) GROUP BY 1,2 ORDER BY 1""", [GLOB]).fetchall()
    hourly = [{"hour": str(x[0]), "platform": x[1], "n": x[2]} for x in r2]
    return {"daily": daily, "hourly": hourly}


def s_distinct():
    r = c.execute("""SELECT count(DISTINCT channel_id) ch, count(DISTINCT user_id) us,
                     count(DISTINCT streamer_name) st FROM read_parquet(?)""", [GLOB]).fetchall()
    return {"channels": r[0][0], "users": r[0][1], "streamers": r[0][2]}


def s_sample_profile():
    files = sorted(x[0] for x in c.execute("SELECT * FROM glob(?)", [GLOB]).fetchall())
    sample = files[::24]
    r = c.execute("""SELECT count(*) n, avg(length(message_text)) avg_len, median(length(message_text)) med_len,
                     quantile_cont(length(message_text), 0.9) p90,
                     sum(CASE WHEN length(message_text)<=3 THEN 1 ELSE 0 END) le3,
                     sum(CASE WHEN message_text LIKE '%ㅋ%' THEN 1 ELSE 0 END) kk
                     FROM read_parquet(?)""", [sample]).fetchall()
    n = r[0][0] or 1
    return {"sample_files": len(sample), "of_files": len(files), "rows": r[0][0],
            "avg_len": round(float(r[0][1] or 0), 2), "med_len": r[0][2], "p90_len": r[0][3],
            "share_le3": round((r[0][4] or 0) / n, 4), "share_kk": round((r[0][5] or 0) / n, 4), "estimate": True}


def s_extract():
    """Exact-target extraction with column projection and a row cap (no SELECT *)."""
    COLS = ("timestamp, platform, channel_id, streamer_name, user_id, nickname, message_type, "
            "message_text, amount_krw")
    where = (f"channel_id='{BJ}' OR streamer_name LIKE '%{BJ_NAME}%' OR lower(user_id) LIKE '"
             + SELF_PREFIX + "%'")
    n = c.execute("SELECT count(*) FROM read_parquet(?) WHERE " + where, [GLOB]).fetchone()[0]
    out = {"rows_total": n, "cap": 3000000}
    if n > 3000000:
        out["skipped"] = "row-cap-exceeded"
        return out
    p1 = os.path.join(BASE, "target_hogi_self.parquet")
    c.execute("COPY (SELECT " + COLS + " FROM read_parquet(?) WHERE " + where + ") TO ? (FORMAT parquet)", [GLOB, p1])
    out.update({"path": p1, "rows": c.execute("SELECT count(*) FROM read_parquet(?)", [p1]).fetchone()[0],
                "bytes": os.path.getsize(p1)})
    return out


for name, fn in [("files", s_files), ("target", s_target), ("hogi_hourly", s_hogi_hourly),
                 ("platform_type", s_platform_type), ("daily_hourly", s_daily_hourly),
                 ("distinct", s_distinct), ("sample_profile", s_sample_profile), ("extract_target", s_extract)]:
    step(name, fn)

log("elapsed", round(time.time() - t0))
bad = [k for k, v in res.items() if isinstance(v, dict) and "error" in v]
with open(OUT, "w") as fh:
    json.dump(res, fh, ensure_ascii=False)
if bad:
    log("failed_steps", bad)
    sys.exit(3)
print("DONE")
