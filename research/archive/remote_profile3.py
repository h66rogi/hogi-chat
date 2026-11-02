import json, os, random, sys, time, traceback
import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pyarrow import fs as pafs

C = CREDS
EP, BK = C["endpoint"], C["bucket"]
PFX = os.environ.get("HOGI_ARCHIVE_PREFIX", "")
GLOB = "s3://" + BK + "/" + PFX + "year=*/month=*/day=*/hour=*/data.parquet"
BASE = os.path.expanduser("~/hogi-chat/archive")
TMP = os.path.join(BASE, "tmp")
OUT = os.path.join(BASE, "profile3_result.json")
os.makedirs(TMP, exist_ok=True)
SECRETS = [C["ak"], C["sk"]]
t0 = time.time()
res = {}


def redact(s):
    s = str(s)
    for x in SECRETS:
        if x:
            s = s.replace(x, "<redacted>")
    return s[:300]


def log(k, v):
    print(json.dumps({k: v}, ensure_ascii=False, default=str), flush=True)


def save():
    with open(OUT, "w") as fh:
        json.dump(res, fh, ensure_ascii=False, default=str)


c = duckdb.connect(config={"memory_limit": "6GB", "threads": 4, "temp_directory": TMP,
                           "max_temp_directory_size": "10GB", "preserve_insertion_order": "false"})
c.execute("LOAD httpfs")
c.execute("CREATE OR REPLACE TEMPORARY SECRET r2 (TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION 'auto', URL_STYLE 'path')",
          [C["ak"], C["sk"], EP.replace("https://", "")])
c.execute("SET http_retries=8; SET http_timeout=120000")
files = sorted(x[0] for x in c.execute("SELECT * FROM glob(?)", [GLOB]).fetchall())
sample = sorted(random.Random(11).sample(files, 49))  # 무작위 시간대(고정 간격은 매번 같은 시각만 뽑힘)
res["n_files"], res["sample_files"] = len(files), len(sample)
res["types"] = [(r[0], r[1]) for r in c.execute("DESCRIBE SELECT * FROM read_parquet(?)", [files[0]]).fetchall()]
res["sample_hours_utc"] = sorted({f.split("hour=")[1][:2] for f in sample})
save()


def step(name, sql, params):
    t = time.time()
    try:
        cur = c.execute(sql, params)
        cols = [d[0] for d in cur.description]
        res[name] = [dict(zip(cols, row)) for row in cur.fetchall()]
        log("ok", {"step": name, "rows": len(res[name]), "secs": round(time.time() - t)})
    except Exception:
        res[name] = {"error": redact(traceback.format_exc().splitlines()[-1])}
        log("fail", {"step": name, "err": res[name]["error"], "secs": round(time.time() - t)})
    save()


# B) sample: context->next-message pairs. A message counts as a usable pair target when the
#    same channel had >= 5 chat lines in the preceding 60 s (enough context to learn from).
step("sample_pairs",
     """WITH t AS (SELECT platform, channel_id, timestamp AS ts FROM read_parquet(?) WHERE message_type = 'chat'),
             w AS (SELECT platform, count(*) OVER (PARTITION BY channel_id ORDER BY ts
                          RANGE BETWEEN INTERVAL 60 SECONDS PRECEDING AND INTERVAL 1 MICROSECONDS PRECEDING) prev60
                   FROM t)
        SELECT platform, count(*) chat_lines,
               sum(CASE WHEN prev60 >= 5 THEN 1 ELSE 0 END) pairs_ctx5,
               sum(CASE WHEN prev60 >= 20 THEN 1 ELSE 0 END) pairs_ctx20
        FROM w GROUP BY 1 ORDER BY chat_lines DESC""", [sample])

# C) sample text profile with pyarrow (no UTF-8 validation on read; decode with replacement)
try:
    t = time.time()
    s3 = pafs.S3FileSystem(access_key=C["ak"], secret_key=C["sk"], endpoint_override=EP, region="auto")
    agg = {}
    rnd = random.Random(7)
    for f in sample:
        tbl = pq.read_table(f.replace("s3://", ""), filesystem=s3,
                            columns=["platform", "message_type", "message_text"])
        mt = tbl.column("message_type").to_pylist()
        pl = tbl.column("platform").to_pylist()
        tx = pc.cast(tbl.column("message_text"), pa.binary()).to_pylist()
        idx = [i for i, m in enumerate(mt) if m == "chat"]
        for i in rnd.sample(idx, min(len(idx), 20000)):
            b = tx[i] or b""
            s = b.decode("utf-8", "replace")
            a = agg.setdefault(pl[i], {"n": 0, "chars": 0, "bytes": 0, "le15": 0, "url": 0, "at": 0, "bad": 0, "lens": []})
            a["n"] += 1
            a["chars"] += len(s)
            a["bytes"] += len(b)
            a["le15"] += len(s) <= 15
            a["url"] += ("http" in s) or ("www." in s)
            a["at"] += s.startswith("@") or " @" in s
            a["bad"] += "\ufffd" in s
            a["lens"].append(len(s))
    out = {}
    for k, a in agg.items():
        L = sorted(a["lens"])
        out[k] = {"n": a["n"], "avg_chars": round(a["chars"] / a["n"], 1), "avg_bytes": round(a["bytes"] / a["n"], 1),
                  "med_chars": L[len(L) // 2], "p90_chars": L[int(len(L) * 0.9)],
                  "share_le15": round(a["le15"] / a["n"], 3), "share_url": round(a["url"] / a["n"], 4),
                  "share_mention": round(a["at"] / a["n"], 4), "share_bad_utf8": round(a["bad"] / a["n"], 5)}
    res["sample_text"] = out
    log("ok", {"step": "sample_text", "secs": round(time.time() - t)})
except Exception:
    res["sample_text"] = {"error": redact(traceback.format_exc().splitlines()[-1])}
    log("fail", {"step": "sample_text", "err": res["sample_text"]["error"]})
save()
log("elapsed", round(time.time() - t0))
print("DONE")
