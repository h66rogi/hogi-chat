import json, os, sys, time, traceback
import duckdb

C = CREDS
EP, BK = C["endpoint"], C["bucket"]
PFX = os.environ.get("HOGI_ARCHIVE_PREFIX", "")
GLOB = "s3://" + BK + "/" + PFX + "year=*/month=*/day=*/hour=*/data.parquet"
BASE = os.path.expanduser("~/hogi-chat/archive")
TMP = os.path.join(BASE, "tmp")
OUT = os.path.join(BASE, "validate_result.json")
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


out = {"endpoint": EP, "bucket": BK, "glob": GLOB}


def save():
    try:
        with open(OUT, "w") as fh:
            json.dump(out, fh, ensure_ascii=False)
    except Exception:
        pass


try:
    def mem():
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, _, v = line.partition(":")
                if k in ("MemTotal", "MemAvailable"):
                    info[k] = int(v.split()[0]) // 1024
        return info
    avail = mem().get("MemAvailable", 0)
    limit = max(1024, min(2048, int(avail * 0.5))) if avail else 1536
    out["meminfo_mb"] = mem()
    out["duckdb_memory_limit_mb"] = limit
    c = duckdb.connect(config={"memory_limit": "%dMB" % limit, "threads": 2, "temp_directory": TMP,
                               "max_temp_directory_size": "4GB", "preserve_insertion_order": "false"})
    c.execute("LOAD httpfs")
    c.execute("CREATE OR REPLACE TEMPORARY SECRET r2 (TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION 'auto', URL_STYLE 'path')",
              [C["ak"], C["sk"], EP.replace("https://", "")])
    log("start", {"available_mb": avail, "limit_mb": limit})
    r = c.execute("SELECT * FROM glob(?)", [GLOB]).fetchall()
    files = sorted(x[0] for x in r)
    out["file_count"] = len(files)
    log("glob", {"n": len(files), "first": files[0].split("/objects/")[-1] if files else None,
                 "last": files[-1].split("/objects/")[-1] if files else None, "secs": round(time.time() - t0)})
    save()
    if not files:
        log("error", "no objects matched")
        sys.exit(2)
    sample = files[len(files) // 2]
    sch = c.execute("DESCRIBE SELECT * FROM read_parquet(?)", [sample]).fetchall()
    out["schema"] = [(x[0], x[1]) for x in sch]
    log("schema", {"ncols": len(sch), "cols": [x[0] for x in sch]})
    save()
    q = c.execute("SELECT count(*), sum(num_rows) FROM parquet_file_metadata(?)", [GLOB]).fetchall()
    out["meta_totals"] = {"files": q[0][0], "rows": q[0][1]}
    log("parquet_file_metadata_totals", {"files": q[0][0], "rows": q[0][1], "secs": round(time.time() - t0)})
    save()
    one = c.execute("""SELECT count(*) n, min(timestamp) t0, max(timestamp) t1, count(DISTINCT channel_id) ch,
                       count(DISTINCT user_id) us FROM read_parquet(?)""", [sample]).fetchall()
    out["sample_hour"] = {"n": one[0][0], "t0": str(one[0][1]), "t1": str(one[0][2]),
                          "channels": one[0][3], "users": one[0][4]}
    log("sample_hour", out["sample_hour"])
    log("elapsed", round(time.time() - t0))
    save()
except Exception:
    log("error", redact(traceback.format_exc().splitlines()[-1]))
    save()
    sys.exit(2)
