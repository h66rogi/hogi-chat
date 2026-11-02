import json, os, sys, time, traceback
import duckdb

C = CREDS
EP, BK = C["endpoint"], C["bucket"]
PFX = os.environ.get("HOGI_ARCHIVE_PREFIX", "")
GLOB = "s3://" + BK + "/" + PFX + "year=*/month=*/day=*/hour=*/data.parquet"
BASE = os.path.expanduser("~/hogi-chat/archive")
TMP = os.path.join(BASE, "tmp")
OUT = os.path.join(BASE, "profile_result.json")
os.makedirs(TMP, exist_ok=True)
SECRETS = [C["ak"], C["sk"]]
t0 = time.time()


def redact(s):
    s = str(s)
    for x in SECRETS:
        if x:
            s = s.replace(x, "<redacted>")
    return s[:400]


def log(k, v):
    print(json.dumps({k: v}, ensure_ascii=False), flush=True)


res = {"glob": GLOB}


def save():
    with open(OUT, "w") as fh:
        json.dump(res, fh, ensure_ascii=False)


c = duckdb.connect(config={"memory_limit": "6GB", "threads": 4, "temp_directory": TMP,
                           "max_temp_directory_size": "10GB", "preserve_insertion_order": "false"})
c.execute("LOAD httpfs")
c.execute("CREATE OR REPLACE TEMPORARY SECRET r2 (TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION 'auto', URL_STYLE 'path')",
          [C["ak"], C["sk"], EP.replace("https://", "")])
c.execute("SET http_retries=8; SET http_timeout=120000")


def step(name, sql, params):
    t = time.time()
    try:
        cur = c.execute(sql, params)
        cols = [d[0] for d in cur.description]
        res[name] = [dict(zip(cols, [str(v) if not isinstance(v, (int, float)) or v is None else v for v in row]))
                     for row in cur.fetchall()]
        log("ok", {"step": name, "rows": len(res[name]), "secs": round(time.time() - t)})
    except Exception:
        res[name] = {"error": redact(traceback.format_exc().splitlines()[-1] or repr(sys.exc_info()[1]))}
        log("fail", {"step": name, "err": res[name]["error"], "secs": round(time.time() - t)})
    save()


# 1) full counts by platform and message type (narrow columns only: platform, message_type, channel_id, user_id)
step("by_platform_type",
     """SELECT platform, message_type, count(*) AS n,
               approx_count_distinct(channel_id) AS channels, approx_count_distinct(user_id) AS users
        FROM read_parquet(?) GROUP BY 1, 2 ORDER BY n DESC""", [GLOB])

# 2) message-length / shape profile on a 1-in-24 file sample (message_text is the heavy column)
files = sorted(x[0] for x in c.execute("SELECT * FROM glob(?)", [GLOB]).fetchall())
sample = files[::24]
res["sample_files"] = len(sample)
step("length_profile_sample",
     """SELECT platform, count(*) n, avg(length(message_text)) avg_len,
               quantile_cont(length(message_text), 0.5) med_len, quantile_cont(length(message_text), 0.9) p90_len,
               avg(CASE WHEN length(message_text) <= 15 THEN 1 ELSE 0 END) share_le15
        FROM read_parquet(?) WHERE message_type = 'chat' GROUP BY 1 ORDER BY n DESC""", [sample])

# 3) channel activity on the sample: how many channels have dense chat (a proxy for usable context->reaction pairs)
step("channel_density_sample",
     """WITH per AS (SELECT platform, channel_id, count(*) n FROM read_parquet(?)
                    WHERE message_type = 'chat' GROUP BY 1, 2)
        SELECT platform, count(*) channels, sum(n) msgs,
               sum(CASE WHEN n >= 1000 THEN 1 ELSE 0 END) ch_ge1000,
               sum(CASE WHEN n >= 1000 THEN n ELSE 0 END) msgs_in_ge1000
        FROM per GROUP BY 1 ORDER BY msgs DESC""", [sample])

log("elapsed", round(time.time() - t0))
save()
print("DONE")
