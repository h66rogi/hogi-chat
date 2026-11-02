import glob, json, os, random, time, traceback
import duckdb
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pyarrow import fs as pafs
from tokenizers import Tokenizer

C = CREDS
EP, BK = C["endpoint"], C["bucket"]
PFX = os.environ.get("HOGI_ARCHIVE_PREFIX", "")
GLOB = "s3://" + BK + "/" + PFX + "year=*/month=*/day=*/hour=*/data.parquet"
BASE = os.path.expanduser("~/hogi-chat/archive")
TMP = os.path.join(BASE, "tmp")
OUT = os.path.join(BASE, "measure_result.json")
SECRETS = [C["ak"], C["sk"]]
res = {}
t0 = time.time()


def redact(s):
    s = str(s)
    for x in SECRETS:
        if x:
            s = s.replace(x, "<redacted>")
    return s[:300]


def log(k, v):
    print(json.dumps({k: v}, ensure_ascii=False, default=str), flush=True)


def save():
    json.dump(res, open(OUT, "w"), ensure_ascii=False, default=str)


c = duckdb.connect(config={"memory_limit": "6GB", "threads": 4, "temp_directory": TMP,
                           "max_temp_directory_size": "10GB", "preserve_insertion_order": "false"})
c.execute("LOAD httpfs")
c.execute("CREATE OR REPLACE TEMPORARY SECRET r2 (TYPE s3, KEY_ID ?, SECRET ?, ENDPOINT ?, REGION 'auto', URL_STYLE 'path')",
          [C["ak"], C["sk"], EP.replace("https://", "")])
c.execute("SET http_retries=8; SET http_timeout=120000")
files = sorted(x[0] for x in c.execute("SELECT * FROM glob(?)", [GLOB]).fetchall())

BJ = os.environ.get("HOGI_BJ_ID", "")
# A) target channel broadcast time: 5-minute buckets (KST) with >= 3 chat lines = live minutes (proxy)
try:
    t = time.time()
    rows = c.execute(f"""
        WITH b AS (SELECT time_bucket(INTERVAL 5 MINUTES, timezone('Asia/Seoul', timestamp)) bk, count(*) n
                   FROM read_parquet(?) WHERE channel_id = '{BJ}' AND message_type = 'chat' GROUP BY 1)
        SELECT strftime(bk, '%Y-%m') mon, count(*) FILTER (WHERE n >= 3) live_buckets,
               count(DISTINCT CAST(bk AS DATE)) FILTER (WHERE n >= 3) live_days,
               sum(n) chat_lines
        FROM b GROUP BY 1 ORDER BY 1""", [files]).fetchall()
    res["hogi_monthly"] = [{"month": r[0], "live_hours": round(r[1] * 5 / 60, 1), "live_days": r[2], "chat_lines": r[3]} for r in rows]
    hrs = c.execute(f"""
        WITH b AS (SELECT time_bucket(INTERVAL 5 MINUTES, timezone('Asia/Seoul', timestamp)) bk, count(*) n
                   FROM read_parquet(?) WHERE channel_id = '{BJ}' AND message_type = 'chat' GROUP BY 1)
        SELECT hour(bk) h, count(*) FROM b WHERE n >= 3 GROUP BY 1 ORDER BY 1""", [files]).fetchall()
    res["hogi_hour_of_day_buckets"] = {int(h): n for h, n in hrs}
    log("ok", {"step": "hogi", "secs": round(time.time() - t)})
except Exception:
    res["hogi_monthly"] = {"error": redact(traceback.format_exc().splitlines()[-1])}
    log("fail", {"step": "hogi", "err": res["hogi_monthly"]["error"]})
save()

# B) real token counts for training examples (context = up to 20 prior lines within 60 s, needs >= 5)
try:
    t = time.time()
    tok_paths = {}
    for p in glob.glob(os.path.join(BASE, "hf", "models--*", "snapshots", "*", "tokenizer.json")):
        name = p.split("models--")[1].split("/")[0].replace("--", "/")
        tok_paths[name] = p
    toks = {n: Tokenizer.from_file(p) for n, p in tok_paths.items()}
    s3 = pafs.S3FileSystem(access_key=C["ak"], secret_key=C["sk"], endpoint_override=EP, region="auto")
    rnd = random.Random(5)
    examples = []
    for f in rnd.sample(files, 4):
        tb = pq.read_table(f.replace("s3://", ""), filesystem=s3,
                           columns=["channel_id", "timestamp", "message_type", "message_text"])
        tb = tb.filter(pc.equal(tb["message_type"], "chat"))
        ch = tb["channel_id"].to_pylist()
        ts = [x.timestamp() for x in tb["timestamp"].to_pylist()]
        tx = [(b or b"").decode("utf-8", "replace") for b in pc.cast(tb["message_text"], pa.binary()).to_pylist()]
        order = sorted(range(len(ch)), key=lambda i: (ch[i], ts[i]))
        by = {}
        for i in order:
            by.setdefault(ch[i], []).append(i)
        cand = []
        for idxs in by.values():
            for k in range(5, len(idxs)):
                j = idxs[k]
                ctx = [idxs[m] for m in range(max(0, k - 20), k) if ts[j] - ts[idxs[m]] <= 60]
                if len(ctx) >= 5:
                    cand.append((ctx, j))
        for ctx, j in rnd.sample(cand, min(2500, len(cand))):
            examples.append(("\n".join(tx[m] for m in ctx), tx[j], len(ctx)))
    out = {"n_examples": len(examples), "avg_ctx_lines": round(sum(e[2] for e in examples) / len(examples), 1),
           "avg_ctx_chars": round(sum(len(e[0]) for e in examples) / len(examples), 1),
           "avg_target_chars": round(sum(len(e[1]) for e in examples) / len(examples), 1)}
    for n, tk in toks.items():
        ctx_t = [len(tk.encode(e[0]).ids) for e in examples]
        tgt_t = [len(tk.encode(e[1]).ids) for e in examples]
        out[n] = {"avg_ctx_tokens": round(sum(ctx_t) / len(ctx_t), 1), "avg_target_tokens": round(sum(tgt_t) / len(tgt_t), 1),
                  "p90_ctx_tokens": sorted(ctx_t)[int(len(ctx_t) * 0.9)],
                  "tokens_per_char": round(sum(ctx_t) / max(1, sum(len(e[0]) for e in examples)), 3)}
    res["tokens"] = out
    log("ok", {"step": "tokens", "secs": round(time.time() - t), "n": len(examples)})
except Exception:
    res["tokens"] = {"error": redact(traceback.format_exc().splitlines()[-1])}
    log("fail", {"step": "tokens", "err": res["tokens"]["error"]})
save()
log("elapsed", round(time.time() - t0))
print("DONE")
