#!/usr/bin/env python3
"""orca-graph — phân việc dạng ĐỒ THỊ PHỤ THUỘC trên PLAN.md, có khoá + lease + generation,
state bền append-only (events.jsonl), và sổ câu trả lời của model có audit nguồn.

Store (mặc định `<overstack>/graph/` — llmwiki/ hoặc .llmwiki/ tuỳ layout, override bằng --dir):
  <id>.graph.json      cache fold từ events (ghi temp → fsync → rename)
  <id>.events.jsonl    append-only {ts,node,from,to,by,op_key,gen,rev,note}
  <id>.answers.jsonl   câu trả lời của MODEL: {q,node,label,score,evidence[],text,ts}
  <id>.locks/<node>    lockfile O_EXCL {gen,lease_until,by}

Lệnh:
  add-node <id> --title T [--depends t1:data,t2] [--blocks t5] [--files a,b] [--verify CMD] [--kind K] [--resources k(mode)] [--mode hitl]
                                    user yêu cầu thêm việc → append khối Task vào PLAN gốc + build lại (plan_version+1) + vẽ lại
  build <PLAN.md> [--id X]          PLAN → graph (deps từ `**Depends:**`, thiếu → suy từ Consumes/Produces "Task N")
  ask <id> needs|parallel|deps <n>|why <n>|related|waiting   (waiting = chờ vì data|gate|resource|queue|control|retry)
  audit-edges <id> [--json] [--strict]   cạnh thiếu lý do / cạnh preference bỏ được / critical path trước-sau — CHỈ ĐỌC
  next <id>                         node ready (đã reaper lease hết → unknown)
  lock|unlock|heartbeat <id> <n> [--by X] [--lease-sec N]
  set <id> <n> <state> [--op-key K] [--gen G] [--if-rev R] [--note ..] [--by X]
  reconcile <id> <n>                chạy `verify` cho node unknown/done_unverified
  sync-orca <id> [--run]            in (hoặc chạy) `orca orchestration task-create --deps` theo topo
  answer <id> <n|-> --q Q --label chắc|gợi-ý|không-biết --score S --evidence E.. --text T
  audit <id>                        mở lại từng nguồn; không mở được = bịa = 0
  show <id>                         tóm tắt state
  reconcile-items --expected A,B,C --verdicts f.jsonl    ghép verdict theo ĐỊNH DANH (PRD v1.1 §25.5); rc 2 khi thiếu/lỗi giao thức
  dry-streak --prev N --complete 0|1 --new N              vòng discovery khô (PRD v1.1 §26.4)
  cost-envelope <id> [--reviewers N] [--max-attempts N]   tổng LẦN GỌI model min/max trước dispatch (PRD v1.1 §27.2)
Luật một dòng của rubric: đúng 1 · sai 0 · không-biết 0.3 · gợi-ý có nguồn thật 0.5 · bịa nguồn 0.
"""
import argparse, contextlib, hashlib, json, os, posixpath, re, shutil, subprocess, sys, time
from pathlib import Path

# GH#166: **Verify:**/**QC:** do người/agent viết theo thói quen bash; shell=True mặc định là /bin/sh → cú pháp
# chỉ-bash (vd `<(...)`) fail rc=2 và bị đọc nhầm là verify đỏ. Có bash thì chạy bằng bash, không thì lùi về sh.
SHELL = shutil.which("bash")

SCHEMA = 1
VERSION = "3.2.1"
STATES = ["proposed", "ready", "locked", "dispatched", "done", "done_unverified",
          "done_user_reported", "failed", "unknown", "blocked"]
TERMINAL_OK = {"done", "done_user_reported"}
LABELS = {"chắc": 1.0, "gợi-ý": 0.5, "không-biết": 0.3}
# PRD v1.1 §23.2: cạnh CỨNG giữ nguyên dù không truyền artifact; `preference` = chỉ do thứ tự viết, audit được đề xuất bỏ.
HARD_REASONS = ("data", "contract", "acceptance", "effect_order", "control")
REASONS = HARD_REASONS + ("preference",)
CLAIM_RE = re.compile(r"^(.+?)\s*(?:\(\s*(shared|exclusive|capacity)\s*(?::\s*(\d+))?\s*\))?$", re.I)
DEP_REASON_RE = re.compile(r"^(.*?)\s*\(\s*([A-Za-z_-]+)\s*\)$")
XDEP_RE = re.compile(r"^[\w.-]+/[A-Za-z0-9]+$")          # dep xuyên graph <gid>/<tid> — chặn token rác có dấu `/` (vd HTML) lọt vào graph
KIND_RE = re.compile(r"^[\w-]{1,32}$")
NO_DEPS = {"—", "-", "–", "none", "không", "n/a"}
def _default_dir() -> Path:
    """Store mặc định: $ORCA_GRAPH_DIR → <overstack>/graph (máy khách .llmwiki/, repo framework llmwiki/; qua overstack_paths
    nếu chạy dưới overstack) → llmwiki/graph. Engine sống ở repo riêng Rheinmir/orca-graph; dưới overstack nó được gọi qua
    SHIM nên `__file__` là đường shim — mọi lookup quanh `__file__` (overstack_paths, control-room) vẫn giải theo layout overstack."""
    import importlib.util
    if os.environ.get("ORCA_GRAPH_DIR"):
        return Path(os.environ["ORCA_GRAPH_DIR"])
    for c in (Path(__file__).resolve().with_name("overstack_paths.py"), Path.home() / ".claude/harness/harness/scripts/overstack_paths.py"):
        if c.is_file():
            try:
                sp = importlib.util.spec_from_file_location("_op", c); m = importlib.util.module_from_spec(sp); sp.loader.exec_module(m)
                d = m.overstack_dir(Path.cwd())
                if d:
                    return Path(d) / "graph"
            except Exception:
                pass
    return Path("llmwiki/graph")   # bare-path: ok fallback khi không có helper (repo framework)


DEFAULT_DIR = _default_dir()


# ---------- store ----------
def now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(f"{p.suffix}.{os.getpid()}.{int(time.time()*1e6)}.tmp")   # tên tmp RIÊNG mỗi process — 2 run song song cùng dir không đạp nhau (race 140926)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text); f.flush(); os.fsync(f.fileno())
    os.replace(tmp, p)


def append_jsonl(p: Path, obj: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n"); f.flush(); os.fsync(f.fileno())


def read_jsonl(p: Path) -> list:
    if not p.exists():
        return []
    out = []
    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
    for i, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            if i == len(lines) - 1:   # dòng cuối cụt do kill giữa chừng → bỏ, không hỏng sổ
                continue
            raise SystemExit(f"events hỏng ở dòng {i+1}: {p}")
    return out


class Store:
    def __init__(self, d: Path, gid: str):
        self.d, self.gid = d, gid
        self.graph_p = d / f"{gid}.graph.json"
        self.events_p = d / f"{gid}.events.jsonl"
        self.answers_p = d / f"{gid}.answers.jsonl"
        self.locks_d = d / f"{gid}.locks"

    def load(self) -> dict:
        if not self.graph_p.exists():
            raise SystemExit(f"không có graph: {self.graph_p}")
        g = json.loads(self.graph_p.read_text(encoding="utf-8"))
        if g.get("schema_version") != SCHEMA:
            raise SystemExit(f"schema_version {g.get('schema_version')} ≠ {SCHEMA} — dừng, không đoán")
        body = {k: v for k, v in g.items() if k != "checksum"}
        if g.get("checksum") and checksum(body) != g["checksum"]:
            print("⚠ checksum lệch — fold lại từ events", file=sys.stderr)
        return fold(g, read_jsonl(self.events_p), self.d)

    def save(self, g: dict) -> None:
        g["schema_version"] = SCHEMA
        g.pop("checksum", None)
        g["checksum"] = checksum(g)
        atomic_write(self.graph_p, json.dumps(g, ensure_ascii=False, indent=1))


def checksum(obj) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def fold(g: dict, events: list, d: Path = None) -> dict:
    """graph.json là cache; events là sự thật. Fold idempotent theo op_key."""
    nodes = {n["id"]: n for n in g["nodes"]}
    seen = set()
    for n in nodes.values():   # fold LUÔN từ gốc — events là sự thật duy nhất, gọi lặp không cộng dồn
        n.update(state="proposed", gen=0, rev=0, attempts=0, fresh="current", verified="unverified")
    g["control"] = "active"
    for e in events:
        k = e.get("op_key")
        if k and k in seen:
            continue
        if k:
            seen.add(k)
        if e.get("kind") == "control":
            g["control"] = e["to"]; continue
        n = nodes.get(e.get("node"))
        if not n:
            continue
        if e.get("kind") == "stale_result":
            continue
        if e.get("to") in STATES:
            n["state"] = e["to"]; n["rev"] = max(n["rev"], e.get("rev", n["rev"]))
            if e.get("gen") is not None:
                n["gen"] = max(n["gen"], e["gen"])
            if e["to"] == "dispatched":
                n["attempts"] = n.get("attempts", 0) + 1
            if e["to"] == "done":
                n["verified"] = "verified"
            if e["to"] in TERMINAL_OK or e["to"] == "done_unverified":
                n["done_spec_hash"] = e.get("spec_hash")
            if e["to"] in ("ready", "proposed"):
                n["verified"] = "unverified"
    # control (PRD §8.3, invariant 11): "đã yêu cầu" chỉ thành "đã dừng" khi không còn node đang chạy
    running = any(n["state"] in ("locked", "dispatched") for n in nodes.values())
    if g["control"] == "pause_requested" and not running:
        g["control"] = "paused"
    if g["control"] == "cancel_requested" and not running:
        g["control"] = "cancelled"
    # join (PRD §4/§8.5, invariant 4): node có child_graph chỉ xong khi MỌI node con xong; con blocked → mẹ blocked
    for n in nodes.values():
        cg = n.get("child_graph")
        if cg and d is not None and n["state"] not in TERMINAL_OK:
            try:
                kids = Store(d, cg).load()["nodes"]
            except SystemExit:
                continue
            if kids and all(k["state"] in TERMINAL_OK for k in kids):
                n["state"] = "done_unverified"; n["verified"] = "unverified"
            elif any(k["state"] == "blocked" for k in kids):
                n["state"] = "blocked"
    # freshness: upstream redo sau khi mình done → stale (cảnh báo, không lùi state)
    for n in nodes.values():
        if n["state"] in TERMINAL_OK:
            up_redo = any(nodes[d]["state"] not in TERMINAL_OK for d in n["deps"] if d in nodes)
            spec_changed = bool(n.get("done_spec_hash")) and n.get("spec_hash") and n["done_spec_hash"] != n["spec_hash"]
            n["fresh"] = "stale" if (up_redo or spec_changed) else "current"
    # ready: proposed mà mọi deps done (dep ngoài `gid/tid` đọc store graph kia; không có store → chưa thoả)
    ext_cache = {}
    def dep_ok(dep):
        if dep in nodes:
            return nodes[dep]["state"] in TERMINAL_OK
        if "/" in dep and d is not None:
            og_, tn = dep.split("/", 1)
            if og_ not in ext_cache:
                try:
                    ext_cache[og_] = {x["id"]: x for x in Store(d, og_).load()["nodes"]}
                except SystemExit:
                    ext_cache[og_] = {}
            return ext_cache[og_].get(tn, {}).get("state") in TERMINAL_OK
        return False
    for n in nodes.values():
        if n["state"] == "proposed" and all(dep_ok(x) for x in n["deps"]):
            n["state"] = "blocked" if n.get("mode") == "hitl" else "ready"
    return g


# ---------- PLAN parser ----------
TASK_RE = re.compile(r"^### Task ([A-Za-z0-9]+)[:\s]+(.*)$")
TASK_REF = re.compile(r"Task ([A-Za-z0-9]+)")


def tid(tok: str) -> str:
    return f"t{tok}" if tok.isdigit() else tok.lower()


def parse_plan(text: str) -> list:
    tasks, cur, fence = [], None, False
    for ln in text.splitlines():
        if ln.strip().startswith("```"):
            fence = not fence; continue
        if fence:                      # nội dung trong khối code không phải cấu trúc PLAN
            continue
        m = TASK_RE.match(ln.strip())
        if m:
            cur = {"id": tid(m.group(1)), "num": tid(m.group(1)), "title": m.group(2).strip(), "files": [],
                   "consumes": [], "produces": [], "deps": [], "deps_conf": "chắc", "kind": "build",
                   "mode": "afk", "verify": "", "qc": "", "produces_for": []}
            tasks.append(cur); continue
        if cur is None or ln.startswith("## "):
            if ln.startswith("## "):
                cur = None
            continue
        s = ln.strip()
        fm = re.match(r"^- (Tạo|Sửa|Test|Xoá|Create|Modify):\s*`([^`]+)`", s)
        if fm:
            cur["files"].append(fm.group(2).split(":")[0]); continue
        for key, field in (("Depends", "deps_raw"), ("Kind", "kind"), ("Mode", "mode"), ("Verify", "verify"), ("QC", "qc"), ("Resources", "resources_raw")):
            km = re.match(rf"^\*\*{key}:\*\*\s*(.*)$", s)
            if km:
                cur[field] = km.group(1).strip().strip("`")
        if s.startswith("- Consumes:"):
            cur["consumes"].append(s[len("- Consumes:"):].strip())
        if s.startswith("- Produces"):
            cur["produces"].append((s.split(":", 1)[1].strip() if ":" in s else "") or s.lstrip("- "))
            cur["produces_for"] += [tid(x) for x in TASK_REF.findall(s)]
    ids = {t["num"] for t in tasks}
    for t in tasks:
        raw = t.pop("deps_raw", "")
        if raw:
            deps, reasons = [], {}
            for tok in [x.strip().strip("`").strip() for x in re.split(r"[,;]", raw)]:
                if not tok or tok.lower() in NO_DEPS:
                    continue
                where = f"Task {t['num']}: Depends `{tok}`"
                rm = DEP_REASON_RE.match(tok)        # `Task 1 (data)` — lý do cạnh, PRD v1.1 §23.2
                reason = ""
                if rm:
                    tok, reason = rm.group(1).strip().strip("`"), normalize_reason(rm.group(2))
                    if reason not in REASONS:
                        raise SystemExit(f"{where}: reason_class lạ `{reason}` — hợp lệ: {', '.join(REASONS)}")
                if "/" in tok:                       # dep XUYÊN graph: <gid>/<tid> (PRD §3.2 milestone có địa chỉ đầy đủ)
                    if not XDEP_RE.match(tok):
                        raise SystemExit(f"{where}: dep xuyên graph phải dạng <graph-id>/<task-id>")
                    x = tok
                else:
                    x = tid(re.sub(r"^Task\s*", "", tok))
                    # KHÔNG nuốt: token không resolve được (ghi chú chen vào, `và`, task không tồn tại) mà bỏ qua im lặng
                    # thì cạnh biến mất, node thành ready sớm, audit-edges cũng không thấy vì cạnh không còn.
                    if x not in ids:
                        raise SystemExit(f"{where}: không resolve được — viết `Task N` hoặc `Task N (reason)`, ngăn bằng dấu phẩy; ghi chú để ở dòng khác")
                    if x == t["num"]:
                        raise SystemExit(f"{where}: task tự phụ thuộc chính nó")
                if x in deps:
                    if reason and reasons.get(x, reason) != reason:
                        raise SystemExit(f"{where}: lặp lại với reason khác (`{reasons[x]}` ≠ `{reason}`) — một cạnh một lý do")
                else:
                    deps.append(x)
                if reason:
                    reasons[x] = reason
            t["deps"] = deps
            if reasons:
                t["dep_reasons"] = reasons
        else:  # suy từ Consumes "Task N" + Produces "dùng bởi Task N" của task khác
            dep = {tid(x) for c in t["consumes"] for x in TASK_REF.findall(c)}
            dep |= {o["num"] for o in tasks if t["num"] in o["produces_for"]}
            t["deps"] = [x for x in sorted(dep) if x in ids and x != t["num"]]
            t["deps_conf"] = "gợi-ý" if t["deps"] else "chắc"
        t["mode"] = "hitl" if t["mode"].lower().startswith("hitl") else "afk"
        t["kind"] = t["kind"].strip().lower()
        if not KIND_RE.match(t["kind"]):
            raise SystemExit(f"Task {t['num']}: **Kind:** `{t['kind'][:40]}` không hợp lệ — một từ [a-z0-9_-]")
        claims = parse_claims(t.pop("resources_raw", ""), f"Task {t['num']}")
        if claims:
            t["resources"] = claims
    for t in tasks:
        for k in ("num", "produces_for"):
            t.pop(k, None)
    return tasks


def normalize_reason(r: str) -> str:
    return r.strip().lower().replace("-", "_").replace("scheduling_preference", "preference")


def parse_claims(raw: str, where: str = "") -> list:
    """`db-migration(exclusive), api-x(shared), llm(capacity:2)` → [{key, mode, units?}] (PRD v1.1 §23.4).
    Key do TOOL chuẩn hoá (lowercase + normpath) để hai node không lách cùng một tài nguyên bằng alias khác chữ."""
    out = {}
    for tok in [x.strip().strip("`") for x in raw.split(",") if x.strip() and x.strip() != "—"]:
        m = CLAIM_RE.match(tok)
        key = posixpath.normpath(m.group(1).strip().lower())
        mode = (m.group(2) or "exclusive").lower()
        if mode == "capacity" and not m.group(3):
            raise SystemExit(f"{where}: claim `{tok}` thiếu số slot — viết `{key}(capacity:N)`")
        c = {"key": key, "mode": mode}
        if mode == "capacity":
            c["units"] = int(m.group(3))
            if c["units"] < 1:
                raise SystemExit(f"{where}: `{tok}` — capacity phải ≥ 1 (0 = node không bao giờ lock được)")
        out[key] = c
    return [out[k] for k in sorted(out)]


def spec_hash(n: dict) -> str:
    """Băm HỢP ĐỒNG của node (title/files/deps/verify/qc/produces) — đổi là node đã-xong thành stale (PRD §7.3, §10.1).
    `qc` (GH#163) nằm trong hợp đồng giống `verify`: đổi lệnh QC cũng phải làm node done cũ thành stale."""
    return hashlib.sha256(json.dumps({k: n.get(k) for k in ("title", "files", "deps", "verify", "qc", "produces")}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def toposort(nodes: dict) -> list:
    """Trả về các LỚP (mỗi lớp = chạy song song được). Raise nếu có cycle."""
    indeg = {i: len([d for d in n["deps"] if d in nodes]) for i, n in nodes.items()}
    layers, done = [], set()
    while len(done) < len(nodes):
        layer = sorted(i for i, d in indeg.items() if d == 0 and i not in done)
        if not layer:
            raise SystemExit("CYCLE: " + ", ".join(i for i in nodes if i not in done))
        layers.append(layer); done |= set(layer)
        for i in layer:
            for j, n in nodes.items():
                if i in n["deps"]:
                    indeg[j] -= 1
    return layers


def write_conflicts(nodes: dict, layers: list) -> list:
    out = []
    for layer in layers:
        for a in layer:
            for b in layer:
                if a < b:
                    shared = sorted(set(nodes[a]["files"]) & set(nodes[b]["files"]))
                    if shared:
                        out.append({"a": a, "b": b, "files": shared})
    return out


def cmd_build(a):
    plan = Path(a.plan)
    text = plan.read_text(encoding="utf-8")
    gid = a.id or plan.stem.replace("-PLAN", "").replace("_PLAN", "")
    tasks = parse_plan(text)
    if not tasks:
        raise SystemExit("PLAN không có `### Task N:` nào")
    if len(tasks) > 20:
        raise SystemExit(f"{len(tasks)} node > 20/graph (PRD §5.2) — gom theo trách nhiệm hoặc tách graph con")
    parent, depth = None, 0
    if a.parent:
        pg, pn = a.parent.split("/", 1)
        pst = Store(Path(a.dir), pg); pgraph = pst.load()
        pnode = {n["id"]: n for n in pgraph["nodes"]}.get(pn)
        if not pnode:
            raise SystemExit(f"graph mẹ {pg} không có node {pn}")
        depth = pgraph.get("depth", 0) + 1
        if depth > 6:
            raise SystemExit(f"depth {depth} > 6 (PRD §5.2) — giảm scope thay vì đào sâu")
        parent = {"graph": pg, "node": pn}
        pnode["child_graph"] = gid; pst.save(pgraph)
    nodes = {t["id"]: t for t in tasks}
    layers = toposort(nodes)
    conflicts = write_conflicts(nodes, layers)
    res_conf = [{"a": x, "b": y, "keys": sorted(claims_clash(nodes[x].get("resources", []), nodes[y].get("resources", [])))}
                for layer in layers for x in layer for y in layer if x < y and claims_clash(nodes[x].get("resources", []), nodes[y].get("resources", []))]
    st = Store(Path(a.dir), gid)
    for t in tasks:
        t["spec_hash"] = spec_hash(t)
    plan_version, superseded = 1, []
    if st.graph_p.exists():                      # REPLAN (PRD §10.1): version mới, không xoá lịch sử (invariant 10)
        old = json.loads(st.graph_p.read_text(encoding="utf-8"))
        old = fold(old, read_jsonl(st.events_p), Path(a.dir))
        plan_version = old.get("plan_version", 1) + 1
        if parent is None and old.get("parent"):   # build lại graph CON mà quên --parent → giữ mẹ cũ, không âm thầm mồ côi
            parent, depth = old["parent"], old.get("depth", 0)
        old_nodes = {n["id"]: n for n in old["nodes"]}
        removed = [i for i in old_nodes if i not in nodes]
        changed = [i for i in nodes if i in old_nodes and old_nodes[i].get("spec_hash") != nodes[i]["spec_hash"]]
        superseded = old.get("superseded", []) + [{"id": i, "title": old_nodes[i]["title"], "state": old_nodes[i]["state"], "plan_version": old.get("plan_version", 1)} for i in removed]
        append_jsonl(st.events_p, {"ts": now(), "kind": "plan.rebuilt", "node": "-", "from": old.get("plan_version", 1), "to": plan_version,
                                   "changed": changed, "removed": removed, "op_key": f"rebuild:{plan_version}"})
        print(f"REPLAN: plan_version {old.get('plan_version', 1)} → {plan_version} · đổi spec {changed or '—'} · bỏ {removed or '—'}")
    g = {"id": gid, "plan": str(plan), "built": now(), "nodes": tasks, "layers": layers, "parent": parent, "depth": depth,
         "plan_version": plan_version, "superseded": superseded, "max_parallel": a.max_parallel, "conflicts": conflicts, "resource_conflicts": res_conf, "links": find_links(Path(a.dir), gid, tasks)}
    g = fold(g, read_jsonl(st.events_p), Path(a.dir))
    st.save(g)
    print(f"graph {gid}: {len(tasks)} node · {len(layers)} lớp · cấp {depth} · {len(conflicts)} xung đột ghi cùng file · {len(g['links'])} liên hệ graph cũ")
    for c in conflicts:
        print(f"  ⚠ {c['a']} ∥ {c['b']} cùng ghi {c['files']} → nên ép tuần tự (thêm **Depends:**)")
    for c in res_conf:
        print(f"  ℹ {c['a']} ∥ {c['b']} cùng claim {c['keys']} → `lock` tự tuần tự hoá lúc chạy, KHÔNG cần thêm Depends (tranh chấp tài nguyên ≠ cạnh DAG, PRD v1.1 §23.2)")
    sug = [t["id"] for t in tasks if t["deps_conf"] == "gợi-ý"]
    if sug:
        print(f"  ℹ deps SUY LUẬN (gợi-ý, chưa khai **Depends:**): {', '.join(sug)}")
    print(f"  → {st.graph_p}")
    if a.strict:
        bad = [t["id"] for t in tasks if not t.get("verify") or not t.get("files")]
        if bad:
            print(f"STRICT: {bad} thiếu verify/files — không dispatch được (PRD §4.4)"); sys.exit(2)


# ---------- add-node: user yêu cầu thêm việc giữa chừng → SỬA PLAN rồi build lại (PLAN vẫn là nguồn, không vá graph.json) ----------
def untid(i: str) -> str:
    return i[1:] if re.fullmatch(r"t\d+", i) else i


def render_task(num: str, a, deps: list) -> str:
    dep_s = ", ".join((f"Task {untid(d)}" if "/" not in d else d) + (f" ({r})" if r else "") for d, r in deps) or "—"
    lines = [f"### Task {num}: {a.title.strip()}", f"**Kind:** {a.kind.strip().lower()}", f"**Depends:** {dep_s}"]
    if a.mode == "hitl":
        lines.append("**Mode:** HITL")
    lines.append("**Files:**")
    lines += [f"- Sửa: `{f.strip()}`" for f in a.files.split(",") if f.strip()]
    lines += ["**Interfaces:**", "- Consumes: —", f"- Produces: {a.produces.strip() or '—'}"]
    if a.resources:
        lines.append(f"**Resources:** {a.resources}")
    if a.verify:
        lines.append(f"**Verify:** `{a.verify.strip().strip('`')}`")
    if a.qc:
        lines.append(f"**QC:** `{a.qc.strip().strip('`')}`")
    return "\n".join(lines) + "\n"


def plan_struct(lines: list) -> list:
    """[(idx, dòng)] của các dòng CẤU TRÚC — bỏ mọi thứ nằm trong code fence, đúng như parse_plan nhìn PLAN.
    add-node phải thấy PLAN bằng đúng con mắt của parser; quét thô thì `### Task 9` hay `**Depends:**` làm MẪU trong fence
    sẽ bị coi là thật (chèn sai chỗ, cấp sai id, sửa nhầm dòng trong fence)."""
    out, fence = [], False
    for i, ln in enumerate(lines):
        if ln.strip().startswith("```"):
            fence = not fence; continue
        if not fence:
            out.append((i, ln))
    return out


def cmd_add_node(a):
    with admission_mutex(Path(a.dir)):          # đọc-sửa-ghi PLAN: 2 add-node song song không được đè mất task của nhau
        _add_node(a)


def _add_node(a):
    st = Store(Path(a.dir), a.id); g = st.load()
    plan = Path(g["plan"])
    if not plan.is_file():
        raise SystemExit(f"không thấy PLAN gốc `{plan}` (đường lưu lúc build, tính từ cwd khi đó) — cd về đúng gốc dự án rồi chạy lại")
    for name in ("title", "depends", "blocks", "files", "verify", "qc", "kind", "produces", "resources"):
        if re.search(r"[\r\n]", getattr(a, name) or ""):   # mỗi field là MỘT dòng PLAN — xuống dòng = chèn được `### Task`/`**Depends:**` giả
            raise SystemExit(f"--{name} chứa xuống dòng — từ chối (mỗi field là một dòng trong PLAN)")
    if not a.title.strip():
        raise SystemExit("--title rỗng")
    text = plan.read_text(encoding="utf-8")
    ids = {n["id"] for n in g["nodes"]}
    if {x["id"] for x in parse_plan(text)} != ids:    # cwd khác có file trùng tên tương đối, hoặc PLAN đã sửa mà chưa build
        raise SystemExit(f"PLAN `{plan}` không khớp graph {a.id} (task trong PLAN ≠ node trong graph) — `build` lại trước, hoặc cd về đúng gốc dự án")
    lines = text.splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    used = ids | {x["id"] for x in g.get("superseded", [])}
    num = str(max([int(i[1:]) for i in used if re.fullmatch(r"t\d+", i)] or [0]) + 1)      # không tái dùng id node đã bị bỏ
    deps = []
    for tok in [x.strip() for x in a.depends.split(",") if x.strip()]:
        d, _, r = tok.partition(":")
        d = d.strip(); r = normalize_reason(r) if r.strip() else ""
        d = d if "/" in d else tid(re.sub(r"^Task\s*", "", d))
        if "/" in d and not XDEP_RE.match(d):
            raise SystemExit(f"--depends: `{d}` — dep xuyên graph phải dạng <graph-id>/<task-id>")
        if "/" not in d and d not in ids:
            raise SystemExit(f"--depends: không có node `{d}` trong {a.id} (có: {sorted(ids)})")
        if r and r not in REASONS:
            raise SystemExit(f"--depends: reason_class lạ `{r}` — hợp lệ: {', '.join(REASONS)}")
        if d not in [x for x, _ in deps]:
            deps.append((d, r))
    parse_claims(a.resources, "--resources")                                             # sai cú pháp thì chết TRƯỚC khi chạm PLAN
    if not KIND_RE.match(a.kind.strip().lower()):
        raise SystemExit(f"--kind `{a.kind}` không hợp lệ — một từ [a-z0-9_-]")
    state = {n["id"]: n["state"] for n in g["nodes"]}
    blocks = []
    for b in [tid(re.sub(r"^Task\s*", "", x.strip())) for x in a.blocks.split(",") if x.strip()]:
        if b not in ids:
            raise SystemExit(f"--blocks: không có node `{b}`")
        if state[b] in ("locked", "dispatched"):                                          # đổi hợp đồng của node ĐANG chạy = kết quả của nó thuộc spec cũ
            raise SystemExit(f"--blocks: {b} đang {state[b]} — chờ nó xong (hoặc `control cancel`) rồi mới chèn việc vào trước nó")
        if b not in blocks:
            blocks.append(b)
    for b in blocks:                                                                      # node mới chèn TRƯỚC b: b phụ thuộc node mới
        lines = _add_dep_line(lines, b, f"Task {num}")
    struct = plan_struct(lines)
    last_task = max(i for i, ln in struct if TASK_RE.match(ln.strip()))
    at = next((i for i, ln in struct if i > last_task and ln.startswith("## ")), len(lines))   # trước mục `## ` đầu tiên sau task cuối (vd ## Origin)
    block = render_task(num, a, deps)
    pre = "" if at == 0 or lines[at - 1].strip() == "" else "\n"
    new_text = "".join(lines[:at]) + pre + block + "\n" + "".join(lines[at:])
    tasks = {x["id"]: x for x in parse_plan(new_text)}                                   # KIỂM trước, ghi sau — bị từ chối thì PLAN nguyên vẹn
    nid = f"t{num}"
    if len(tasks) > 20:
        raise SystemExit(f"{len(tasks)} node > 20/graph (PRD §5.2) — tách graph con: build <PLAN con> --parent {a.id}/<node>")
    toposort(tasks)
    if set(tasks) != ids | {nid}:
        raise SystemExit("PLAN sau khi chèn không ra đúng `node cũ + 1 node mới` — dừng, không ghi")
    got = tasks[nid]
    if got["deps"] != [d for d, _ in deps] or (a.verify.strip() and not got["verify"]) or got["files"] != [f.strip() for f in a.files.split(",") if f.strip()]:
        raise SystemExit(f"node mới parse lại không khớp tham số (deps={got['deps']} verify={got['verify']!r} files={got['files']}) — dừng, không ghi")
    miss = [b for b in blocks if nid not in tasks[b]["deps"]]
    if miss:
        raise SystemExit(f"--blocks: chèn xong mà {miss} vẫn không phụ thuộc {nid} — dừng, không ghi")
    atomic_write(plan, new_text)
    print(f"+ {nid} «{a.title.strip()}» → {plan}" + (f" · chèn trước {blocks}" if blocks else ""))
    par = g.get("parent")
    cmd_build(argparse.Namespace(plan=str(plan), id=a.id, dir=a.dir, parent=f"{par['graph']}/{par['node']}" if par else None,
                                 strict=False, max_parallel=g.get("max_parallel", 4)))
    append_jsonl(st.events_p, {"ts": now(), "kind": "node.added", "node": nid, "by": a.by, "title": a.title.strip(), "blocks": blocks,
                               "op_key": f"add-node:{nid}:{g.get('plan_version', 1) + 1}"})
    if not a.no_viz:
        run_viz(st.graph_p)


def _add_dep_line(lines: list, nid: str, ref: str) -> list:
    """Thêm `ref` vào **Depends:** của task `nid` (chỉ nhìn dòng CẤU TRÚC, ngoài code fence); chưa có dòng Depends thì thêm ngay dưới tiêu đề."""
    struct = plan_struct(lines)
    start = next(i for i, ln in struct if (m := TASK_RE.match(ln.strip())) and tid(m.group(1)) == nid)
    end = next((i for i, ln in struct if i > start and (TASK_RE.match(ln.strip()) or ln.startswith("## "))), len(lines))
    for i, ln in struct:
        if start < i < end:
            m = re.match(r"^(\*\*Depends:\*\*\s*)(.*?)(\s*)$", ln)
            if m:
                cur = m.group(2).strip()
                lines[i] = f"{m.group(1)}{ref if cur.lower() in NO_DEPS | {''} else cur + ', ' + ref}\n"
                return lines
    return lines[:start + 1] + [f"**Depends:** {ref}\n"] + lines[start + 1:]


def run_viz(graph_p: Path) -> None:
    """Vẽ lại HTML của graph (graph-viz.py nằm cạnh engine hoặc ở fdk/tools) — in `→ <path>` cho user mở."""
    here = Path(__file__).resolve()
    for c in (here.with_name("graph-viz.py"), here.parents[2] / "fdk/tools/graph-viz.py", Path.home() / ".claude/harness/fdk/tools/graph-viz.py"):
        if c.is_file():
            subprocess.call([sys.executable, str(c), str(graph_p)]); return
    print("(không thấy graph-viz.py — bỏ qua bước vẽ)", file=sys.stderr)


def find_links(d: Path, gid: str, tasks: list) -> list:
    """Liên hệ graph cũ = khớp ARTIFACT (file) — tất định. Ngữ nghĩa để model trả lời + gắn nhãn."""
    mine = {f for t in tasks for f in t["files"]}
    links = []
    for p in sorted(d.glob("*.graph.json")):
        if p.stem == f"{gid}.graph":
            continue
        try:
            og = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        for on in og.get("nodes", []):
            shared = sorted(mine & set(on.get("files", [])))
            if shared:
                links.append({"to_graph": og["id"], "to_node": on["id"], "via": shared, "kind": "touches-same-file"})
    return links


def cmd_check_cycles(a):
    """Gộp MỌI graph trong dir thành một DAG khoá gid/tid; in ĐƯỜNG cycle cụ thể (PRD §5.3), rc 2."""
    d = Path(a.dir); edges = {}
    for p in sorted(d.glob("*.graph.json")):
        g = json.loads(p.read_text(encoding="utf-8"))
        for n in g["nodes"]:
            edges[f"{g['id']}/{n['id']}"] = [x if "/" in x else f"{g['id']}/{x}" for x in n["deps"]]
    color, path, found = {}, [], []
    def dfs(u):
        color[u] = 1; path.append(u)
        for v in edges.get(u, []):
            if v not in edges:
                continue
            if color.get(v) == 1:
                found.append(path[path.index(v):] + [v]); return True
            if color.get(v, 0) == 0 and dfs(v):
                return True
        path.pop(); color[u] = 2; return False
    for u in list(edges):
        if color.get(u, 0) == 0 and dfs(u):
            break
    if found:
        print("CYCLE: " + " → ".join(found[0])); sys.exit(2)
    print(f"OK: {len(edges)} node trong {len(list(d.glob('*.graph.json')))} graph, không cycle")


# ---------- leaf contract (PRD §4.4) ----------
def leaf_gaps(n: dict) -> list:
    """Leaf chỉ hợp lệ khi đủ: outcome · phạm vi ghi · output · phép kiểm quan sát được · deps rõ."""
    gaps = []
    if not n.get("title"):
        gaps.append("title (outcome)")
    if not n.get("files"):
        gaps.append("files (phạm vi ghi)")
    if not any(x.strip("—- ") for x in n.get("produces", [])):
        gaps.append("produces (output)")
    if not n.get("verify"):
        gaps.append("verify (phép kiểm)")
    if n.get("deps_conf") == "gợi-ý":
        gaps.append("deps suy luận (chưa khai Depends)")
    return gaps


def cmd_lint(a):
    g = Store(Path(a.dir), a.id).load()
    ok = 0
    for n in g["nodes"]:
        gaps = leaf_gaps(n)
        ok += not gaps
        print(f"  {'✓' if not gaps else '✗'} {n['id']:<4} {n['title'][:48]:<50} {'; '.join(gaps)}")
    print(f"{ok}/{len(g['nodes'])} leaf đủ hợp đồng (PRD §4.4)")


# ---------- topology audit (PRD v1.1 §23.3) — CHỈ ĐỌC, không sửa PLAN/graph ----------
def critical_path(nodes: dict, skip=frozenset()) -> list:
    """Đường dài nhất theo SỐ NODE (unit-weight — không phải thời lượng; PRD §23.3: chưa có duration thì phải gắn nhãn)."""
    best = {}
    def walk(i):
        if i not in best:
            ups = [walk(d) for d in nodes[i]["deps"] if d in nodes and (d, i) not in skip]
            best[i] = max(ups, key=len, default=[]) + [i]
        return best[i]
    return max((walk(i) for i in nodes), key=len, default=[])


def audit_edges(g: dict) -> dict:
    nodes = {n["id"]: n for n in g["nodes"]}
    findings, removable = [], []
    for n in g["nodes"]:
        for dep in n["deps"]:
            reason = (n.get("dep_reasons") or {}).get(dep)
            eid = f"{dep}->{n['id']}"
            if reason is None:
                code = "EDGE_INFERRED" if n.get("deps_conf") == "gợi-ý" else "EDGE_UNJUSTIFIED"
                findings.append({"edge": eid, "code": code, "action": "keep",
                                 "why": "cạnh chưa có reason_class — GIỮ; thiếu lý do không chứng minh hai node độc lập (§23.3 bước 6)"})
            elif reason == "preference":
                up = nodes.get(dep)
                hidden = []
                if up is None:
                    hidden.append("dep xuyên graph — không soi được hợp đồng bên kia")
                else:
                    if any(tid(x) == dep for c in n.get("consumes", []) for x in TASK_REF.findall(c)):
                        hidden.append(f"Consumes của {n['id']} nhắc tới {dep} → thực ra là cạnh data")
                    hay = " ".join(n.get("consumes", []) + [n.get("verify", ""), n.get("qc", "")])
                    outs = set(up.get("files", [])) | {Path(f).name for f in up.get("files", [])}
                    for pr in up.get("produces", []):
                        outs |= {pr.strip()} | set(re.findall(r"`([^`]+)`", pr))
                    used = sorted(o for o in outs if len(o.strip("—- ")) >= 4 and o in hay)
                    if used:
                        hidden.append(f"Consumes/Verify của {n['id']} dùng output của {dep}: {used[:3]} → thực ra là cạnh data")
                    shared = sorted(set(n.get("files", [])) & set(up.get("files", [])))
                    if shared:
                        hidden.append(f"cùng ghi {shared} → bỏ cạnh sẽ thành xung đột ghi")
                    clash = sorted(claims_clash(n.get("resources", []), up.get("resources", [])))
                    if clash:
                        hidden.append(f"cùng claim exclusive {clash} → khai **Resources:** là đủ tuần tự hoá, vẫn không bỏ mù")
                if hidden and not (len(hidden) == 1 and hidden[0].startswith("cùng claim")):
                    findings.append({"edge": eid, "code": "PREFERENCE_HAS_HIDDEN_CONSTRAINT", "action": "keep", "why": "; ".join(hidden)})
                else:
                    removable.append((dep, n["id"]))
                    findings.append({"edge": eid, "code": "PREFERENCE_REMOVABLE", "action": "propose_remove",
                                     "why": "chỉ là thứ tự viết" + (f" ({hidden[0]})" if hidden else "")})
    skip = frozenset(removable)
    after_nodes = {i: {**n, "deps": [d for d in n["deps"] if (d, i) not in skip]} for i, n in nodes.items()}
    before, after = critical_path(nodes), critical_path(nodes, skip)
    return {"graph": g["id"], "plan_version": g.get("plan_version", 1), "edges": sum(len(n["deps"]) for n in g["nodes"]),
            "findings": findings, "diff": {"remove_preference_edges": [f"{a}->{b}" for a, b in removable]},
            "critical_path": {"unit": "node-count (unit-weight, KHÔNG phải thời lượng)", "before": before, "after": after},
            "layers": {"before": len(toposort(nodes)), "after": len(toposort(after_nodes))},
            "frontier": {"before": toposort(nodes)[0] if nodes else [], "after": toposort(after_nodes)[0] if nodes else []}}


def claims_clash(a: list, b: list) -> set:
    """Key mà hai bộ claim không được giữ cùng lúc: trùng key và ít nhất một bên exclusive (PRD §23.4)."""
    bm = {c["key"]: c["mode"] for c in b}
    return {c["key"] for c in a if c["key"] in bm and "exclusive" in (c["mode"], bm[c["key"]])}


def cmd_audit_edges(a):
    r = audit_edges(Store(Path(a.dir), a.id).load())
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=1))
    else:
        print(f"audit-edges {r['graph']} v{r['plan_version']}: {r['edges']} cạnh · dry-run, KHÔNG sửa plan (PRD v1.1 §23.3)")
        for f in r["findings"]:
            print(f"  {f['code']:<34} {f['edge']:<14} [{f['action']}] {f['why']}")
        cp = r["critical_path"]
        print(f"  critical path ({cp['unit']}): {len(cp['before'])} → {len(cp['after'])}   {' → '.join(cp['before'])}  ⇒  {' → '.join(cp['after'])}")
        print(f"  số lớp: {r['layers']['before']} → {r['layers']['after']} · chạy ngay từ đầu: {r['frontier']['before']} → {r['frontier']['after']}")
        if r["diff"]["remove_preference_edges"]:
            print(f"  đề xuất bỏ: {r['diff']['remove_preference_edges']} — sửa **Depends:** trong PLAN rồi build lại nếu đồng ý")
    if a.strict and any(f["code"] == "EDGE_UNJUSTIFIED" for f in r["findings"]):
        sys.exit(2)


# ---------- ask ----------
def upstream(nodes, nid, acc=None):
    acc = acc if acc is not None else []
    for d in nodes[nid]["deps"]:
        if d in nodes and d not in acc:
            acc.append(d); upstream(nodes, d, acc)
    return acc


def downstream(nodes, nid):
    return [i for i, n in nodes.items() if nid in upstream(nodes, i)]


def wait_reasons(g: dict, d: Path) -> dict:
    """{node: {reason, detail}} cho mọi node CHƯA chạy & chưa xong — PRD v1.1 §28.4: "chờ vì data / gate / resource / queue".
    reason ∈ data | gate | resource | queue | control | retry | none (none = lock được ngay)."""
    nodes = {n["id"]: n for n in g["nodes"]}
    running = [n["id"] for n in g["nodes"] if n["state"] in ("locked", "dispatched")]
    out = {}
    for n in g["nodes"]:
        s = n["state"]
        if s in TERMINAL_OK or s in ("locked", "dispatched"):
            continue
        if g.get("control", "active") != "active":
            out[n["id"]] = {"reason": "control", "detail": f"control={g['control']} — resume trước"}
        elif s == "blocked":
            out[n["id"]] = {"reason": "gate", "detail": "HITL — cần người quyết, không dispatch headless"}
        elif s == "done_unverified":
            out[n["id"]] = {"reason": "gate", "detail": "verify/QC chưa xanh — reconcile hoặc làm lại"}
        elif s in ("failed", "unknown") and n.get("attempts", 0) >= 3:
            out[n["id"]] = {"reason": "retry", "detail": f"{s} sau {n['attempts']} attempt — hết lượt, cần người"}
        elif s == "unknown":
            out[n["id"]] = {"reason": "gate", "detail": "lease hết, chưa biết kết quả — reconcile trước khi lock lại"}
        elif s == "proposed":
            pend = [x for x in n["deps"] if nodes.get(x, {}).get("state") not in TERMINAL_OK]
            gates = [x for x in pend if (n.get("dep_reasons") or {}).get(x) in ("control", "acceptance")]
            out[n["id"]] = ({"reason": "gate", "detail": f"chờ cổng duyệt/nghiệm thu từ {gates}"} if gates and len(gates) == len(pend)
                            else {"reason": "data", "detail": f"chờ upstream {pend}"})
        else:                                                  # ready | failed còn lượt
            rb = resource_blockers(g, n, d)
            if rb:
                out[n["id"]] = {"reason": "resource", "detail": "; ".join(f"{b['key']}({b['why']}) do {', '.join(b['held_by'])} giữ" for b in rb)}
            elif len(running) >= g.get("max_parallel", 4):
                out[n["id"]] = {"reason": "queue", "detail": f"max_parallel={g.get('max_parallel', 4)} đầy ({', '.join(running)})"}
            else:
                out[n["id"]] = {"reason": "none", "detail": "lock được ngay"}
    return out


def print_waiting(g: dict, d: Path) -> None:
    w = wait_reasons(g, d)
    for nid, r in w.items():
        print(f"  {nid:<5} chờ {r['reason']:<9} {r['detail']}")
    tally = {}
    for r in w.values():
        tally[r["reason"]] = tally.get(r["reason"], 0) + 1
    done = sum(1 for n in g["nodes"] if n["state"] in TERMINAL_OK)
    print(f"  tổng: {done}/{len(g['nodes'])} xong · " + (" · ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "không node nào chờ"))


def cmd_ask(a):
    g = Store(Path(a.dir), a.id).load()
    nodes = {n["id"]: n for n in g["nodes"]}
    q = a.query[0]
    if q == "needs":
        for l, layer in enumerate(g["layers"]):
            for i in layer:
                n = nodes[i]
                print(f"[L{l}] {i} {n['title']}  ({n['kind']}/{n['mode']}, {n['state']})  files={len(n['files'])}")
    elif q == "parallel":
        for l, layer in enumerate(g["layers"]):
            tag = " ⚠xung đột" if any({c['a'], c['b']} <= set(layer) for c in g["conflicts"]) else ""
            print(f"L{l}: {' ∥ '.join(layer)}{tag}")
        rn = [i for i, n in nodes.items() if n["state"] == "ready"]
        print(f"chạy NGAY được: {' ∥ '.join(rn) or '(không có)'}")
    elif q in ("deps", "why"):
        nid = a.query[1]
        n = nodes[nid]
        if q == "deps":
            print(f"{nid} phụ thuộc TRỰC TIẾP: {n['deps'] or '—'} [{n['deps_conf']}]")
            print(f"gián tiếp: {upstream(nodes, nid) or '—'}")
            print(f"mở khoá cho: {downstream(nodes, nid) or '—'}")
        else:
            print(f"{nid} — {n['title']}")
            print(f"  làm ra: {n['produces'] or '—'}")
            print(f"  mở khoá: {downstream(nodes, nid) or '— (lá; giá trị nằm ở chính output)'}")
            print(f"  chạm file: {n['files']}")
            ls = [l for l in g["links"] if any(f in l["via"] for f in n["files"])]
            print(f"  liên hệ graph cũ (khớp file): {[(l['to_graph'], l['to_node']) for l in ls] or '—'}")
    elif q == "related":
        for l in g["links"]:
            print(f"{g['id']} → {l['to_graph']}/{l['to_node']} via {l['via']} [{l['kind']}]")
        if not g["links"]:
            print("không khớp artifact với graph cũ nào (liên hệ ngữ nghĩa → model trả lời, nhãn gợi-ý)")
    elif q == "waiting":
        print_waiting(g, Path(a.dir))
    else:
        raise SystemExit("ask: needs|parallel|deps <n>|why <n>|related|waiting")


# ---------- runtime ----------
def reaper(st: Store, g: dict, by="reaper") -> int:
    nodes = {n["id"]: n for n in g["nodes"]}
    n_exp = 0
    if st.locks_d.exists():
        for lp in st.locks_d.iterdir():
            try:
                lk = json.loads(lp.read_text())
            except Exception:
                continue
            if time.time() > lk["lease_until"] and nodes.get(lp.name, {}).get("state") in ("locked", "dispatched"):
                emit(st, g, lp.name, "unknown", by=by, note="lease hết — không tự kết luận", op_key=f"reap:{lp.name}:{lk['gen']}")
                lp.unlink(); n_exp += 1
    return n_exp


def emit(st: Store, g: dict, nid: str, to: str, by="", note="", op_key="", gen=None, if_rev=None, plan_version=None) -> bool:
    nodes = {n["id"]: n for n in g["nodes"]}
    n = nodes[nid]
    if to not in STATES:
        raise SystemExit(f"state lạ: {to} (hợp lệ: {STATES})")
    op_key = op_key or f"{nid}:{to}:{int(time.time()*1000)}"
    if any(e.get("op_key") == op_key for e in read_jsonl(st.events_p)):
        print(f"no-op: op_key {op_key} đã có"); return False
    if if_rev is not None and n["rev"] != if_rev:
        raise SystemExit(f"CAS: rev hiện tại {n['rev']} ≠ --if-rev {if_rev}")
    if to in TERMINAL_OK or to == "done_unverified":
        if plan_version is not None and plan_version != g.get("plan_version", 1):
            append_jsonl(st.events_p, {"ts": now(), "kind": "stale_result", "node": nid, "plan_version": plan_version, "cur_plan_version": g.get("plan_version", 1), "by": by})
            print(f"STALE: kết quả plan_version {plan_version} ≠ hiện tại {g.get('plan_version', 1)} — không publish (invariant 2)"); return False
        if gen is not None and gen != n["gen"]:
            append_jsonl(st.events_p, {"ts": now(), "kind": "stale_result", "node": nid, "gen": gen, "cur_gen": n["gen"], "by": by})
            print(f"STALE: kết quả gen {gen} ≠ gen hiện tại {n['gen']} — không publish"); return False
        if to == "done" and n.get("verify") and by != "reconcile":
            rc = subprocess.call(n["verify"], shell=True, executable=SHELL)
            if rc != 0:
                to = "done_unverified"; note = (note + f" verify rc={rc}").strip()
        if to == "done" and not n.get("verify"):
            to = "done_unverified"; note = (note + " (không có verify)").strip()
        if to == "done" and n.get("qc") and by != "reconcile":
            rc = subprocess.call(n["qc"], shell=True, executable=SHELL)
            if rc != 0:
                to = "done_unverified"; note = (note + f" qc rc={rc}").strip()
    new_gen = n["gen"] + 1 if to == "dispatched" else n["gen"]
    frm, rev0 = n["state"], n["rev"]
    ev = {"ts": now(), "node": nid, "from": frm, "to": to, "by": by, "op_key": op_key, "gen": new_gen, "rev": n["rev"] + 1, "note": note,
          "spec_hash": n.get("spec_hash"), "plan_version": g.get("plan_version", 1)}
    append_jsonl(st.events_p, ev)
    st.save(fold(g, read_jsonl(st.events_p), st.d))
    print(f"{nid}: {frm} → {to} (gen {new_gen}, rev {rev0+1})")
    if to in ("locked", "dispatched"):
        # Luồng GÕ TAY lock → set dispatched (luồng chính của SKILL) trước đây không đăng ký store và không bật daemon — chỉ `run`
        # làm việc đó. Hệ quả đo 200926: cả một phiên 12 node không ai canh lease, control-room chỉ vẽ lại khi state đổi rồi
        # đứng im. Node bắt đầu chạy bằng đường nào thì cũng phải có người canh.
        registry_add(st.d); spawn_daemon()
    regen_room([st.d])
    return True


def cmd_next(a):
    st = Store(Path(a.dir), a.id); g = st.load()
    k = reaper(st, g) + reap_store(st.d, skip=a.id)
    if k:
        g = st.load(); print(f"reaper: {k} node hết lease → unknown")
    if g.get("control", "active") != "active":
        print(f"control={g['control']} — không cấp node mới" + (" (còn node đang chạy, chờ chúng kết thúc)" if g["control"].endswith("_requested") else "")); return
    rn = [n for n in g["nodes"] if n["state"] == "ready"]
    hitl = [n["id"] for n in g["nodes"] if n["state"] == "blocked"]
    print("ready (chạy song song ngay): " + (" ∥ ".join(n["id"] for n in rn) or "(không có)"))
    if hitl:
        print(f"blocked (HITL — cần người, KHÔNG dispatch headless): {hitl}")
    held = {i: r for i, r in wait_reasons(g, st.d).items() if r["reason"] in ("resource", "queue")}
    for i, r in held.items():                                  # ready ≠ admitted (PRD v1.1 §8.4): có input rồi vẫn chờ slot/claim
        print(f"  {i} ready nhưng CHƯA lock được — chờ {r['reason']}: {r['detail']}")
    left = [n["id"] for n in g["nodes"] if n["state"] not in TERMINAL_OK]
    if not left:
        print("✅ graph hoàn tất")


@contextlib.contextmanager
def admission_mutex(d: Path, wait: float = 15.0):
    """Một mutex cho cả store trong lúc KIỂM + GHI (`lock`: claim → `locked`; `add-node`: đọc-sửa-ghi PLAN).
    Lấy hết claim hoặc không lấy gì (không hold-and-wait, PRD v1.1 §23.4). Dùng flock: kernel tự nhả khi process chết
    nên KHÔNG có logic phá khoá stale (bản O_EXCL + mtime từng có TOCTOU: A phá khoá cũ rồi tạo khoá mới, B đã stat thấy
    stale từ trước phá luôn khoá mới của A — đo 1/150 vòng với 6 process).
    shortcut: mutex toàn store, đổi sang khoá theo từng resource_key nếu nhiều agent lock dồn dập thấy chờ."""
    d.mkdir(parents=True, exist_ok=True)
    gi = d / ".gitignore"                            # file khoá là RUNTIME — store thường nằm trong git của dự án, đừng làm bẩn cây
    if not gi.exists():
        gi.write_text("# orca-graph runtime — không track\n.admission.lock*\n*.locks/\n", encoding="utf-8")
    lp = d / ".admission.lock"; t0 = time.time()
    try:
        import fcntl
    except ImportError:                              # Windows python thuần: không có flock → O_EXCL, chấp nhận phải xoá tay nếu crash
        fcntl = None
    if fcntl:
        with open(lp, "a") as fh:
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB); break
                except OSError:
                    if time.time() - t0 > wait:
                        raise SystemExit(f"admission bận quá {wait:.0f}s ({lp}) — có lệnh lock/add-node khác đang chạy; thử lại")
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
        return
    while True:
        try:
            os.close(os.open(str(lp) + ".excl", os.O_CREAT | os.O_EXCL | os.O_WRONLY)); break
        except FileExistsError:
            if time.time() - t0 > wait:
                raise SystemExit(f"admission bận quá {wait:.0f}s — nếu không còn lệnh nào chạy, xoá {lp}.excl")
            time.sleep(0.05)
    try:
        yield
    finally:
        Path(str(lp) + ".excl").unlink(missing_ok=True)


def reap_store(d: Path, skip: str = "") -> int:
    """Reaper cho MỌI graph trong store — claim xuyên graph: lease hết ở graph A mà không ai `next A` thì B kẹt mãi."""
    k = 0
    for p in sorted(Path(d).glob("*.graph.json")):
        gid = p.name[:-len(".graph.json")]
        if gid == skip:
            continue
        try:
            st = Store(Path(d), gid); k += reaper(st, st.load())
        except SystemExit:
            continue
    return k


def resource_blockers(g: dict, n: dict, d: Path) -> list:
    """Claim của `n` đang bị node locked|dispatched nào giữ — soi MỌI graph trong store (tài nguyên dùng chung xuyên graph)."""
    mine = n.get("resources") or []
    if not mine:
        return []
    holders = []                                    # (gid/nid, claim)
    for p in sorted(Path(d).glob("*.graph.json")):
        gid = p.name[:-len(".graph.json")]
        try:
            og_ = g if gid == g["id"] else Store(Path(d), gid).load()
        except SystemExit:
            continue
        for x in og_["nodes"]:
            if x["state"] in ("locked", "dispatched") and not (gid == g["id"] and x["id"] == n["id"]):
                holders += [(f"{gid}/{x['id']}", c) for c in x.get("resources") or []]
    out = []
    for c in mine:
        same = [(who, h) for who, h in holders if h["key"] == c["key"]]
        excl = [(who, h) for who, h in same if "exclusive" in (c["mode"], h["mode"])]
        # capacity:N = KÍCH THƯỚC POOL của key. Mỗi holder (shared hay capacity) chiếm 1 slot; các node khai N khác nhau thì
        # lấy N NHỎ NHẤT — không phụ thuộc thứ tự lock, và khai `shared` không lách được quota người khác đã khai.
        caps = [h["units"] for _, h in same if h["mode"] == "capacity"] + ([c["units"]] if c["mode"] == "capacity" else [])
        if excl:
            out.append({"key": c["key"], "mode": c["mode"], "held_by": [w for w, _ in excl], "why": "exclusive"})
        elif caps and len(same) >= min(caps):
            out.append({"key": c["key"], "mode": c["mode"], "held_by": [w for w, _ in same], "why": f"capacity {len(same)}/{min(caps)} đầy"})
    return out


def cmd_lock(a):
    with admission_mutex(Path(a.dir)):
        _lock(a)


def _lock(a):
    st = Store(Path(a.dir), a.id); g = st.load()
    if reaper(st, g) + reap_store(st.d, skip=a.id):
        g = st.load()
    nodes = {n["id"]: n for n in g["nodes"]}
    n = nodes[a.node]
    if n["state"] not in ("ready", "unknown", "failed"):
        raise SystemExit(f"{a.node} đang {n['state']} — chỉ lock node ready/unknown/failed")
    if g.get("control", "active") != "active":
        raise SystemExit(f"control={g['control']} — không lock (resume trước)")
    running = sum(1 for x in g["nodes"] if x["state"] in ("locked", "dispatched"))
    if running >= g.get("max_parallel", 4):
        raise SystemExit(f"max_parallel={g.get('max_parallel', 4)} đã đầy ({running} node đang chạy) — chờ node xong (PRD §12.1)")
    if n["attempts"] >= a.max_attempts and n["state"] != "ready":
        raise SystemExit(f"{a.node} đã {n['attempts']} attempt ≥ {a.max_attempts} — dừng retry")
    rb = resource_blockers(g, n, st.d)
    if rb:
        raise SystemExit(f"{a.node} chờ RESOURCE (không phải chờ data): " + "; ".join(f"{b['key']}({b['why']}) đang do {', '.join(b['held_by'])} giữ" for b in rb) + " — lock lại khi node đó xong (PRD v1.1 §23.4)")
    st.locks_d.mkdir(parents=True, exist_ok=True)
    lp = st.locks_d / a.node
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise SystemExit(f"{a.node} đã bị khoá: {lp.read_text()}")
    with os.fdopen(fd, "w") as f:
        json.dump({"gen": n["gen"] + 1, "lease_until": time.time() + a.lease_sec, "by": a.by}, f); f.flush(); os.fsync(f.fileno())
    emit(st, g, a.node, "locked", by=a.by, note=f"lease {a.lease_sec}s", op_key=a.op_key)


def cmd_unlock(a):
    st = Store(Path(a.dir), a.id)
    lp = st.locks_d / a.node
    if lp.exists():
        lp.unlink(); print(f"unlock {a.node}")
    else:
        print("không có lock")
    g = st.load()
    n = {x["id"]: x for x in g["nodes"]}.get(a.node)
    if n and n["state"] in ("locked", "dispatched"):     # gỡ khoá mà để nguyên state = node "đang chạy" mãi mãi, giữ luôn claim của nó
        emit(st, g, a.node, "unknown", by=getattr(a, "by", "") or "unlock", note="unlock tay khi node đang chạy — reconcile trước khi lock lại",
             op_key=f"unlock:{a.node}:{n['gen']}:{n['rev']}")


def cmd_heartbeat(a):
    st = Store(Path(a.dir), a.id)
    lp = st.locks_d / a.node
    if not lp.exists():
        raise SystemExit("không có lock để gia hạn")
    lk = json.loads(lp.read_text()); lk["lease_until"] = time.time() + a.lease_sec
    atomic_write(lp, json.dumps(lk)); print(f"heartbeat {a.node} → +{a.lease_sec}s")


def cmd_set(a):
    st = Store(Path(a.dir), a.id); g = st.load()
    ok = emit(st, g, a.node, a.state, by=a.by, note=a.note, op_key=a.op_key, gen=a.gen, if_rev=a.if_rev, plan_version=a.plan_version)
    if ok and a.state in TERMINAL_OK | {"failed", "done_unverified"}:
        cmd_unlock(a)


def cmd_reconcile(a):
    st = Store(Path(a.dir), a.id); g = st.load()
    n = {x["id"]: x for x in g["nodes"]}[a.node]
    if not n.get("verify"):
        print(f"{a.node} không có verify → không tự kết luận được; cần người: set done_user_reported hoặc ready"); return
    rc = subprocess.call(n["verify"], shell=True, executable=SHELL)
    to = "done" if rc == 0 else "ready"
    note = f"verify rc={rc}"
    qrc = None
    if to == "done" and n.get("qc"):
        qrc = subprocess.call(n["qc"], shell=True, executable=SHELL)
        if qrc != 0:
            to = "ready"; note += f" | qc rc={qrc} FAIL"
    op_key = f"reconcile:{a.node}:{n['gen']}:{rc}" + (f":{qrc}" if qrc is not None else "")
    emit(st, g, a.node, to, by="reconcile", note=note, op_key=op_key)
    if to != "done":
        cmd_unlock(a)


def cmd_sync_orca(a):
    g = Store(Path(a.dir), a.id).load()
    idmap, cmds = {}, []
    for layer in g["layers"]:
        for i in layer:
            n = {x["id"]: x for x in g["nodes"]}[i]
            deps = json.dumps([idmap.get(d, f"<{d}>") for d in n["deps"]])
            spec = f"[graph {g['id']}/{i}] {n['title']}"
            cmd = ["orca", "orchestration", "task-create", "--spec", spec, "--task-title", n["title"][:60], "--deps", deps, "--json"]
            if a.run:
                out = subprocess.run(cmd, capture_output=True, text=True)
                try:
                    idmap[i] = json.loads(out.stdout)["result"]["task"]["id"]   # result.task.id, KHÔNG envelope id
                except Exception:
                    raise SystemExit(f"task-create lỗi cho {i}: {out.stdout[:200]} {out.stderr[:200]}")
                print(f"{i} → {idmap[i]}")
            else:
                cmds.append(" ".join(json.dumps(c) if " " in c else c for c in cmd))
    if not a.run:
        print("# dry-run (thêm --run để tạo thật; sổ Orca là runtime-global — đóng dấu dự án qua orca-reconcile.py --stamp nếu có):")
        print("\n".join(cmds))


# ---------- daemon: run wrapper · registry · watch (PRD §8.4: lease 60 s · heartbeat 15 s · reaper 15 s) ----------
def home() -> Path:
    h = Path(os.environ.get("ORCA_GRAPH_HOME") or Path.home() / ".orca-graph"); h.mkdir(parents=True, exist_ok=True); return h


def registry_load() -> dict:
    p = home() / "registry.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"dirs": [], "max_running": 4}


def registry_save(r: dict) -> None:
    atomic_write(home() / "registry.json", json.dumps(r, ensure_ascii=False, indent=1))


def registry_add(d: Path) -> None:
    r = registry_load(); k = str(d.resolve())
    if k not in r["dirs"]:
        r["dirs"].append(k); registry_save(r)


def running_nodes(d: Path) -> list:
    """[(gid, node)] đang locked|dispatched trong một dir — chi phí tỉ lệ số graph trong dir."""
    out = []
    for p in sorted(Path(d).glob("*.graph.json")):
        try:
            g = Store(Path(d), p.name[:-len(".graph.json")]).load()
        except SystemExit:
            continue
        out += [(g, n) for n in g["nodes"] if n["state"] in ("locked", "dispatched")]
    return out


def registry_prune() -> dict:
    r = registry_load()
    r["dirs"] = [d for d in r["dirs"] if Path(d).is_dir() and running_nodes(Path(d))]
    registry_save(r); return r


def daemon_alive() -> int:
    lp = home() / "daemon.lock"
    try:
        pid = int(lp.read_text().strip()); os.kill(pid, 0); return pid
    except (OSError, ValueError):
        return 0


def daemon_lock() -> bool:
    lp = home() / "daemon.lock"
    if daemon_alive():
        return False
    lp.unlink(missing_ok=True)
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as f:
        f.write(str(os.getpid()))
    return True


def spawn_daemon() -> None:
    if os.environ.get("ORCA_GRAPH_NO_DAEMON") or daemon_alive():
        return
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch"], stdin=subprocess.DEVNULL,
                     stdout=open(home() / "watch.log", "a"), stderr=subprocess.STDOUT, start_new_session=True)


def _git_root(cwd: Path):
    """Repo git gốc của cwd, None nếu không trong git repo (vd tmp_path test) — bỏ qua enforcement khi None."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True)
        return Path(r.stdout.strip()) if r.returncode == 0 else None
    except OSError:
        return None


def _changed_files(root: Path) -> dict:
    """{path: "M"|"??"} — tracked đã sửa vs untracked mới, đường dẫn tương đối gốc git (khớp quy ước `files` PLAN.md)."""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True)
    out = {}
    for ln in r.stdout.splitlines():
        if len(ln) < 4:
            continue
        code, path = ln[:2], ln[3:].split(" -> ")[-1].strip()
        if path:
            out[path] = "??" if code.strip() == "??" else "M"
    return out


def enforce_allowed_paths(root: Path, before: dict, allowed: list, strict: bool, ignore_prefix: str = "") -> list:
    """GH#162: chunk ghi NGOÀI `files` khai trong node — cảnh báo (mặc định) hoặc phục hồi cứng (--strict).
    Chỉ xét file MỚI đổi trong lần chạy này (after − before), không đụng đổi có từ trước khi spawn.
    `ignore_prefix` = thư mục store orca-graph (.graph.json/.events.jsonl/.locks) — bookkeeping của CHÍNH
    engine, không phải agent tự ghi, heartbeat rewrite lock file liên tục trong lúc chạy sẽ tự lộ ra đây
    nếu không loại trừ."""
    after = _changed_files(root)
    touched = {p: c for p, c in after.items() if p not in before and not (ignore_prefix and p.startswith(ignore_prefix))}
    out_of_scope = [p for p in touched if p not in set(allowed)]
    if out_of_scope and strict:
        for p in out_of_scope:
            if touched[p] == "??":
                try:
                    (root / p).unlink()
                except OSError:
                    pass
            else:
                subprocess.call(["git", "checkout", "--", p], cwd=root)
    return out_of_scope


def cmd_run(a):
    """Chạy MỘT node headless: lock ngắn → dispatched → spawn lệnh → heartbeat theo pid → done/failed tự động."""
    st = Store(Path(a.dir), a.id); g = st.load()
    n = {x["id"]: x for x in g["nodes"]}[a.node]
    if not n.get("verify") and not a.allow_unverified:
        raise SystemExit(f"{a.node} không có verify — không chạy headless (PRD §4.4); thêm **Verify:** hoặc --allow-unverified")
    if not a.cmd:
        raise SystemExit("thiếu lệnh: run <id> <node> -- <cmd...>")
    a.by = a.by or f"run:{os.getpid()}"; a.op_key = ""; a.max_attempts = 3
    cmd_lock(a)
    g = st.load()
    emit(st, g, a.node, "dispatched", by=a.by, note=" ".join(a.cmd)[:120], op_key=f"run:{a.node}:{int(time.time()*1000)}")
    registry_add(Path(a.dir)); spawn_daemon()
    g0 = st.load()
    gen = {x["id"]: x for x in g0["nodes"]}[a.node]["gen"]; pv = g0.get("plan_version", 1)
    lp = st.locks_d / a.node
    root = _git_root(Path.cwd())
    before = _changed_files(root) if root else {}
    p = subprocess.Popen(a.cmd)
    while p.poll() is None:
        time.sleep(a.hb)
        if lp.exists():
            lk = json.loads(lp.read_text()); lk["lease_until"] = time.time() + a.lease_sec; lk["pid"] = p.pid; atomic_write(lp, json.dumps(lk))
    g = st.load()
    to = "done" if p.returncode == 0 else "failed"
    note = f"rc={p.returncode}"
    if to == "done" and root is not None and n.get("files"):
        store_rel = os.path.relpath(str(Path(a.dir).resolve()), str(root))
        oos = enforce_allowed_paths(root, before, n["files"], a.strict, ignore_prefix=store_rel + os.sep if not store_rel.startswith("..") else "")
        if oos:
            tag = "strict: revert" if a.strict else "⚠ ngoài phạm vi (files)"
            note += f" | {tag} {len(oos)}: {','.join(oos[:5])}"
    ok = emit(st, g, a.node, to, by=a.by, note=note, op_key=f"run-end:{a.node}:{gen}", gen=gen, plan_version=pv if to == "done" else None)
    if not ok and to == "done":      # replan xảy ra GIỮA lúc chạy: kết quả thuộc plan cũ, không publish (invariant 2) → làm lại theo spec mới
        g = st.load()
        if {x["id"]: x for x in g["nodes"]}.get(a.node, {}).get("state") == "dispatched":
            emit(st, g, a.node, "ready", by=a.by, note=f"kết quả plan v{pv} bị loại sau replan — chạy lại", op_key=f"run-stale:{a.node}:{gen}")
        to = "stale"
    cmd_unlock(a); registry_prune()
    sys.exit(0 if to == "done" else p.returncode or 1)


def _room_builder(hints=()) -> "Path | None":   # chuỗi: engine phải chạy được cả trên Python 3.9
    """Builder của DỰ ÁN chứa graph trước, rồi cwd, rồi cây cạnh engine, cuối cùng bản cài global.

    Vì sao phải đi từ thư mục graph: daemon (`watch_once`) chạy với cwd BẤT KỲ, còn engine cài ở
    ~/.orca-graph nên `__file__`.parents[2] không có fdk/tools/ → cả hai neo kia đều trượt xuống bản
    global ~/.claude/harness (cũ hơn repo). Bug thật 20/09/2026: mỗi lượt daemon vẽ đè cockpit bằng CSS
    cũ, trang kanban mọc lại sọc viền một cạnh sau khi repo đã sửa — và không ai thấy vì daemon im lặng."""
    seen, cands = set(), []
    for d in list(hints) + [Path.cwd()]:
        d = Path(d).resolve()
        for anc in [d, *d.parents]:
            if anc in seen:
                break
            seen.add(anc); cands.append(anc / "fdk/tools/build-control-room.py")
    cands += [Path(__file__).resolve().parents[2] / "fdk/tools/build-control-room.py",
              Path.home() / ".claude/harness/fdk/tools/build-control-room.py"]
    return next((c for c in cands if c.is_file()), None)


def regen_room(hints=()) -> None:
    """Vẽ lại cockpit (rẻ ~100 ms) — gọi ở MỌI lần state đổi (emit) + mỗi lượt daemon/run để trang LIVE.
    stdout KHÔNG bị nuốt: build-control-room.py tự in 3 dòng `→ <path>` (cockpit/detail/kanban) —
    đó là cách duy nhất path lộ ra cho user, không dựa vào model tự nhớ."""
    if os.environ.get("ORCA_GRAPH_NO_ROOM"):
        return
    br = _room_builder(hints)
    if br:
        subprocess.call([sys.executable, str(br)], stderr=subprocess.DEVNULL)


def watch_once(build_room: bool = True) -> int:
    r = registry_prune(); total = 0; changed = False
    print(f"[watch {time.strftime('%H:%M:%S')}] {len(r['dirs'])} dir đang có node chạy · trần toàn máy {r.get('max_running', 4)}")
    for d in r["dirs"]:
        d = Path(d)
        for p in sorted(d.glob("*.graph.json")):
            gid = p.name[:-len(".graph.json")]; st = Store(d, gid)
            try:
                g = st.load()
            except SystemExit:
                continue
            k = reaper(st, g)
            if k:
                changed = True; g = st.load()
            for n in g["nodes"]:
                if n["state"] == "unknown" and n.get("verify"):
                    rc = subprocess.call(n["verify"], shell=True, executable=SHELL)
                    emit(st, g, n["id"], "done" if rc == 0 else "ready", by="reconcile", note=f"watch reconcile rc={rc}", op_key=f"reconcile:{n['id']}:{n['gen']}:{rc}")
                    print(f"  reconcile {gid}/{n['id']} → {'done' if rc == 0 else 'ready'}"); changed = True; g = st.load()
                if n["state"] in ("locked", "dispatched"):
                    lk = {}
                    try:
                        lk = json.loads((st.locks_d / n["id"]).read_text())
                    except (OSError, ValueError):
                        pass
                    left = int(lk.get("lease_until", 0) - time.time())
                    print(f"  {gid:<32} {n['id']:<5} {n['state']:<10} lease còn {left:>5}s  gen {n['gen']}"); total += 1
    if total > r.get("max_running", 4):
        print(f"  ⚠ {total} node đang chạy > trần toàn máy {r.get('max_running', 4)}")
    if build_room:
        regen_room(r["dirs"])  # mỗi lượt, không chỉ khi reaper đổi state — lease còn/số node chạy đổi liên tục
    return total


def cmd_watch(a):
    try:
        sys.stdout.reconfigure(line_buffering=True)   # chạy nền redirect vào watch.log → không được buffer
    except Exception:
        pass
    if a.once:                       # một lượt thủ công: op_key idempotent nên chạy song song daemon vẫn an toàn
        watch_once(); return
    if not daemon_lock():
        raise SystemExit(f"daemon đã chạy (pid {daemon_alive()})")
    idle_since = None
    try:
        while True:
            n = watch_once()
            if a.once:
                return
            idle_since = None if n else (idle_since or time.time())
            if idle_since and time.time() - idle_since > a.idle_sec:
                print("registry rỗng quá lâu — daemon thoát"); return
            time.sleep(a.interval)
    finally:
        (home() / "daemon.lock").unlink(missing_ok=True)


# ---------- answers + audit ----------
EV_RE = re.compile(r"^(file|event|edge|absence|cmd):(.+)$")


def cmd_answer(a):
    st = Store(Path(a.dir), a.id); st.load()
    if a.label not in LABELS:
        raise SystemExit(f"label phải là {list(LABELS)}")
    if a.label != "không-biết" and not a.evidence:
        raise SystemExit("chắc/gợi-ý BẮT BUỘC có --evidence (file:path:line | event:<op_key> | edge:<a>-><b> | absence:<lệnh>)")
    for e in a.evidence:
        if not EV_RE.match(e):
            raise SystemExit(f"evidence sai dạng: {e}")
    score = min(float(a.score), LABELS[a.label])
    append_jsonl(st.answers_p, {"ts": now(), "q": a.q, "node": a.node, "label": a.label, "self_score": score,
                                "evidence": a.evidence, "text": a.text, "by": a.by})
    print(f"ghi câu trả lời [{a.label}] tự chấm {score} · {len(a.evidence)} nguồn")


def check_evidence(e: str, st: Store, g: dict) -> bool:
    kind, body = EV_RE.match(e).groups()
    if kind == "file":
        m = re.match(r"^(.*?)(?::(\d+))?$", body)
        p = Path(m.group(1))
        if not p.exists():
            return False
        if m.group(2):
            return int(m.group(2)) <= len(p.read_text(encoding="utf-8", errors="ignore").splitlines())
        return True
    if kind == "event":
        return any(ev.get("op_key") == body or ev.get("ts") == body for ev in read_jsonl(st.events_p))
    if kind == "edge":
        m = re.match(r"^(?:([^/]+)/)?(\w+)->(\w+)$", body)
        if not m:
            return False
        gg = g if not m.group(1) or m.group(1) == g["id"] else Store(st.d, m.group(1)).load()
        nodes = {n["id"]: n for n in gg["nodes"]}
        return m.group(3) in nodes and m.group(2) in nodes[m.group(3)]["deps"]
    if kind in ("absence", "cmd"):   # lệnh đã chạy — chạy lại phải KHÔNG lỗi cú pháp (rc≠127); absence: output rỗng
        r = subprocess.run(body, shell=True, executable=SHELL, capture_output=True, text=True)
        return r.returncode != 127 and (kind == "cmd" or not r.stdout.strip())
    return False


def cmd_audit(a):
    st = Store(Path(a.dir), a.id); g = st.load()
    answers = read_jsonl(st.answers_p)
    if not answers:
        print("chưa có câu trả lời"); return
    tot_self = tot_audit = 0.0
    rows = []
    for ans in answers:
        bad = [e for e in ans["evidence"] if not check_evidence(e, st, g)]
        if ans["label"] == "không-biết":
            sc = 0.3
        elif bad:
            sc = 0.0   # bịa = 0, bất kể nhãn
        else:
            sc = ans["self_score"]
        tot_self += ans["self_score"]; tot_audit += sc
        rows.append((ans["q"], ans.get("node") or "-", ans["label"], ans["self_score"], sc, bad))
        print(f"{'BỊA ' if bad else 'OK  '} {ans['q']:<9} {ans.get('node') or '-':<5} [{ans['label']}] tự={ans['self_score']} audit={sc}" + (f"  nguồn hỏng: {bad}" if bad else ""))
    n = len(answers)
    print(f"— {n} câu · tự chấm {tot_self:.1f} · sau audit {tot_audit:.1f} · khoảng cách {tot_self - tot_audit:.1f} (lớn = bịa/tự tin quá)")
    append_jsonl(st.d / "audit-log.jsonl", {"ts": now(), "graph": g["id"], "n": n, "self": tot_self, "audit": tot_audit})


# ---------- PRD v1.1 §25.5 / §26.4 / §27.2: helper THUẦN (chép nguyên văn PRD) + lệnh bọc ----------
def reconcile_verdicts(expected_ids, verdict_rows):
    """Ghép verdict theo ĐỊNH DANH, không theo vị trí mảng. Chỉ kiểm giao thức — KHÔNG xác minh chứng cứ, không tự đánh graph xong."""
    from collections.abc import Mapping
    if not isinstance(expected_ids, (list, tuple)):
        raise ValueError("INVALID_MANIFEST")
    if any(not isinstance(x, str) or not x for x in expected_ids):
        raise ValueError("INVALID_ITEM_ID")
    if len(set(expected_ids)) != len(expected_ids):
        raise ValueError("DUPLICATE_MANIFEST_ID")
    expected = set(expected_ids)
    outcomes = {}
    for row in verdict_rows:
        if not isinstance(row, Mapping):
            raise ValueError("MALFORMED_VERDICT")
        item_id = row.get("item_id")
        if not isinstance(item_id, str) or item_id not in expected:
            raise ValueError("UNEXPECTED_ITEM_ID")
        if item_id in outcomes:
            raise ValueError("DUPLICATE_VERDICT")
        verdict = row.get("verdict")
        if not isinstance(verdict, str) or verdict not in {
            "supported", "refuted", "inconclusive"
        }:
            raise ValueError("INVALID_VERDICT")
        outcomes[item_id] = verdict
    return {
        "supported_ids": [x for x in expected_ids if outcomes.get(x) == "supported"],
        "refuted_ids": [x for x in expected_ids if outcomes.get(x) == "refuted"],
        "inconclusive_ids": [x for x in expected_ids if outcomes.get(x) == "inconclusive"],
        "missing_ids": [x for x in expected_ids if x not in outcomes],
        "all_results_received": set(outcomes) == expected,
    }


def next_dry_streak(previous, *, round_complete, new_count):
    """Vòng discovery "khô" chỉ tính khi vòng HOÀN TẤT và không có ứng viên mới; vòng lỗi đưa streak về 0."""
    if type(previous) is not int or previous < 0:
        raise ValueError("INVALID_PREVIOUS_STREAK")
    if type(round_complete) is not bool:
        raise ValueError("INVALID_COMPLETENESS")
    if type(new_count) is not int or new_count < 0:
        raise ValueError("INVALID_NEW_COUNT")
    if not round_complete or new_count > 0:
        return 0
    return previous + 1


INFRA_OK = ("ok", None)


def cmd_reconcile_items(a):
    """Manifest đã seal (expected IDs) × các dòng verdict → thiếu/thừa/trùng theo TẬP ĐỊNH DANH (PRD v1.1 §24.1).
    Dòng có status hạ tầng ≠ ok hoặc verdict null = outcome THIẾU có ID (errored_ids) — không bị lọc mất, không tính refuted."""
    def fail(code):
        print(json.dumps({"protocol_error": code}, ensure_ascii=False)); sys.exit(2)
    expected = [x.strip() for x in (Path(a.expected_file).read_text(encoding="utf-8").replace("\n", ",") if a.expected_file else a.expected).split(",") if x.strip()]
    if not expected and not a.allow_empty:
        fail("EMPTY_MANIFEST")           # "không có input" cần policy tường minh (--allow-empty); không tự suy ra "đủ hết"
    if not Path(a.verdicts).is_file():
        fail("VERDICTS_FILE_NOT_FOUND")  # thiếu file ≠ 0 dòng verdict
    rows = read_jsonl(Path(a.verdicts))
    is_err = lambda r: isinstance(r, dict) and (r.get("status") not in INFRA_OK or r.get("verdict") is None)
    err_ids = [r.get("item_id") for r in rows if is_err(r)]
    ok_ids = [r.get("item_id") for r in rows if isinstance(r, dict) and not is_err(r)]
    if any(not isinstance(i, str) or i not in expected for i in err_ids):
        fail("UNEXPECTED_ITEM_ID")       # dòng lỗi cũng phải thuộc manifest — ID lạ không được lặng lẽ bỏ qua
    if set(err_ids) & set(ok_ids) or len(set(err_ids)) != len(err_ids):
        fail("CONFLICTING_OUTCOME")      # cùng item vừa có verdict vừa có dòng lỗi (hoặc hai dòng lỗi) → không tự chọn bên nào
    try:
        out = reconcile_verdicts(expected, [r for r in rows if not is_err(r)])
    except ValueError as e:
        fail(str(e))
    out["errored_ids"] = [x for x in out["missing_ids"] if x in err_ids]
    out["counts"] = {"expected": len(expected), "received": len(expected) - len(out["missing_ids"])}
    # hai mẫu số KHÁC nhau (PRD v1.1 §24.1): xong/đã-chọn và đã-chọn/toàn-tập. Không biết toàn tập thì ghi unknown, không báo 100%.
    out["coverage"] = {"completed/selected": f"{out['counts']['received']}/{len(expected)}",
                       "selected/known_universe": f"{len(expected)}/{a.universe if a.universe is not None else 'unknown'}"}
    print(json.dumps(out, ensure_ascii=False, indent=1))
    if not out["all_results_received"]:
        print(f"THIẾU {out['missing_ids']} — {out['counts']['received']}/{out['counts']['expected']}; đủ SỐ LƯỢNG dòng không có nghĩa đủ ĐỊNH DANH", file=sys.stderr); sys.exit(2)


def cmd_dry_streak(a):
    try:
        s = next_dry_streak(a.prev, round_complete=bool(a.complete), new_count=a.new)
    except ValueError as e:
        print(str(e)); sys.exit(2)
    print(json.dumps({"dry_streak": s, "converged_heuristic": s >= a.stop_after,
                      "note": "khô = heuristic dừng dưới strategy/snapshot đã dùng, KHÔNG chứng minh đã rà hết (PRD v1.1 §26.1)"}, ensure_ascii=False))


def cost_envelope(g: dict, reviewers: int = 1, max_attempts: int = 3) -> dict:
    """Đếm LẦN GỌI MODEL trước khi dispatch (PRD v1.1 §27.2) — không nói "N agent" thay tổng call.
    worker = mỗi node afk 1 call/attempt · reviewer = node có **QC:** × reviewers · verify = lệnh shell tất định (0 token, vẫn tốn CPU)."""
    afk = [n for n in g["nodes"] if n.get("mode") != "hitl"]
    qc = [n for n in afk if n.get("qc")]
    base = len(afk) + reviewers * len(qc)
    return {"graph": g["id"], "nodes": len(g["nodes"]), "planner_calls": 1, "worker_calls": len(afk), "reviewer_calls": reviewers * len(qc),
            "synthesis_calls": 1, "min_calls": 1 + base + 1, "max_calls": 1 + max_attempts * base + 1, "expected_calls": "unknown — chưa có số đo tỉ lệ retry",
            "spent_dispatches": sum(n.get("attempts", 0) for n in g["nodes"]), "human_gates": len(g["nodes"]) - len(afk),
            "deterministic_checks": sum(1 for n in g["nodes"] if n.get("verify")), "deterministic_note": "model tokens 0, vẫn tốn CPU/I-O — không ghi tổng cost = 0",
            "assumptions": {"reviewers_per_qc_node": reviewers, "max_attempts": max_attempts, "price": "unknown — không tự cho ngân sách vô hạn"}}


def cmd_cost_envelope(a):
    print(json.dumps(cost_envelope(Store(Path(a.dir), a.id).load(), a.reviewers, a.max_attempts), ensure_ascii=False, indent=1))


def cmd_control(a):
    st = Store(Path(a.dir), a.id); g = st.load()
    cur = g.get("control", "active")
    if a.action == "status":
        print(f"control={cur}"); return
    to = {"pause": "pause_requested", "cancel": "cancel_requested", "resume": "active"}[a.action]
    if a.action == "resume" and cur == "cancelled":
        raise SystemExit("đã cancelled — không resume; build lại plan version mới nếu muốn tiếp")
    append_jsonl(st.events_p, {"ts": now(), "kind": "control", "node": "-", "from": cur, "to": to, "by": a.by, "op_key": f"control:{to}:{int(time.time()*1000)}"})
    g = st.load(); st.save(g)
    print(f"control: {cur} → {g['control']}" + (" (yêu cầu đã nhận; workload chưa dừng)" if g["control"].endswith("_requested") else ""))


def cmd_show(a):
    g = Store(Path(a.dir), a.id).load()
    print(f"{g['id']}  plan={g['plan']}  v{g.get('plan_version',1)}  control={g.get('control','active')}  max_parallel={g.get('max_parallel',4)}  nodes={len(g['nodes'])}  layers={len(g['layers'])}  cấp={g.get('depth',0)}  superseded={[x['id'] for x in g.get('superseded',[])]}" + (f"  mẹ={g['parent']['graph']}/{g['parent']['node']}" if g.get('parent') else ""))
    for n in g["nodes"]:
        print(f"  {n['id']:<4} {n['state']:<18} gen={n['gen']} rev={n['rev']} att={n['attempts']} {n['fresh']:<7} deps={n['deps']}{' child=' + n['child_graph'] if n.get('child_graph') else ''}  {n['title'][:50]}")


# ---------- cli ----------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--version", action="version", version=f"orca-graph {VERSION}")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("build"); p.add_argument("plan"); p.add_argument("--id"); p.add_argument("--parent", help="<gid>/<node> graph mẹ"); p.add_argument("--strict", action="store_true"); p.add_argument("--max-parallel", type=int, default=4); p.set_defaults(f=cmd_build)
    p = sp.add_parser("ask"); p.add_argument("id"); p.add_argument("query", nargs="+"); p.set_defaults(f=cmd_ask)
    p = sp.add_parser("next"); p.add_argument("id"); p.set_defaults(f=cmd_next)
    for name, fn in (("lock", cmd_lock), ("unlock", cmd_unlock), ("heartbeat", cmd_heartbeat), ("reconcile", cmd_reconcile)):
        p = sp.add_parser(name); p.add_argument("id"); p.add_argument("node"); p.add_argument("--by", default=os.environ.get("USER", "agent"))
        p.add_argument("--lease-sec", type=int, default=5400); p.add_argument("--max-attempts", type=int, default=3); p.add_argument("--op-key", default="")
        p.set_defaults(f=fn)
    p = sp.add_parser("set"); p.add_argument("id"); p.add_argument("node"); p.add_argument("state")
    p.add_argument("--op-key", default=""); p.add_argument("--gen", type=int); p.add_argument("--if-rev", type=int); p.add_argument("--plan-version", type=int)
    p.add_argument("--note", default=""); p.add_argument("--by", default=os.environ.get("USER", "agent")); p.set_defaults(f=cmd_set)
    p = sp.add_parser("sync-orca"); p.add_argument("id"); p.add_argument("--run", action="store_true"); p.set_defaults(f=cmd_sync_orca)
    p = sp.add_parser("answer"); p.add_argument("id"); p.add_argument("node"); p.add_argument("--q", required=True, choices=["needs", "parallel", "deps", "why", "related"])
    p.add_argument("--label", required=True); p.add_argument("--score", default="1"); p.add_argument("--evidence", nargs="*", default=[])
    p.add_argument("--text", required=True); p.add_argument("--by", default="model"); p.set_defaults(f=cmd_answer)
    p = sp.add_parser("audit"); p.add_argument("id"); p.set_defaults(f=cmd_audit)
    p = sp.add_parser("check-cycles"); p.add_argument("cdir", nargs="?"); p.set_defaults(f=lambda a: (setattr(a, "dir", a.cdir or a.dir), cmd_check_cycles(a)))
    p = sp.add_parser("show"); p.add_argument("id"); p.set_defaults(f=cmd_show)
    p = sp.add_parser("run"); p.add_argument("id"); p.add_argument("node")
    p.add_argument("--hb", type=float, default=15); p.add_argument("--lease-sec", type=int, default=60); p.add_argument("--allow-unverified", action="store_true"); p.add_argument("--by", default="")
    p.add_argument("--strict", action="store_true", help="GH#162: phục hồi cứng file ghi ngoài `files` khai (mặc định chỉ cảnh báo trong note)")
    p.set_defaults(f=cmd_run)
    p = sp.add_parser("watch"); p.add_argument("--once", action="store_true"); p.add_argument("--interval", type=float, default=5); p.add_argument("--idle-sec", type=int, default=600); p.set_defaults(f=cmd_watch)
    p = sp.add_parser("lint"); p.add_argument("id"); p.set_defaults(f=cmd_lint)
    p = sp.add_parser("audit-edges"); p.add_argument("id"); p.add_argument("--json", action="store_true"); p.add_argument("--strict", action="store_true", help="rc 2 khi còn EDGE_UNJUSTIFIED"); p.set_defaults(f=cmd_audit_edges)
    p = sp.add_parser("control"); p.add_argument("id"); p.add_argument("action", choices=["pause", "resume", "cancel", "status"]); p.add_argument("--by", default=os.environ.get("USER", "agent")); p.set_defaults(f=cmd_control)
    p = sp.add_parser("reconcile-items", help="manifest đã seal × verdict rows → thiếu/thừa/trùng theo ĐỊNH DANH; rc 2 khi thiếu hoặc lỗi giao thức")
    p.add_argument("--expected", default=""); p.add_argument("--expected-file", default=""); p.add_argument("--verdicts", required=True); p.add_argument("--universe", type=int, help="số item ĐÃ BIẾT của toàn tập nguồn (bỏ trống = unknown)")
    p.add_argument("--allow-empty", action="store_true", help="manifest rỗng là hợp lệ (mặc định: lỗi EMPTY_MANIFEST)"); p.set_defaults(f=cmd_reconcile_items)
    p = sp.add_parser("dry-streak"); p.add_argument("--prev", type=int, required=True); p.add_argument("--complete", type=int, choices=[0, 1], required=True)
    p.add_argument("--new", type=int, required=True); p.add_argument("--stop-after", type=int, default=2); p.set_defaults(f=cmd_dry_streak)
    p = sp.add_parser("cost-envelope"); p.add_argument("id"); p.add_argument("--reviewers", type=int, default=1); p.add_argument("--max-attempts", type=int, default=3); p.set_defaults(f=cmd_cost_envelope)
    p = sp.add_parser("add-node", help="thêm node theo yêu cầu user: append khối Task vào PLAN gốc rồi build lại (plan_version+1)")
    p.add_argument("id"); p.add_argument("--title", required=True); p.add_argument("--depends", default="", help="t1:data,t2 (reason tuỳ chọn)")
    p.add_argument("--blocks", default="", help="t5,t6 — các node này sẽ PHỤ THUỘC node mới (chèn vào giữa)")
    p.add_argument("--files", default=""); p.add_argument("--verify", default=""); p.add_argument("--qc", default=""); p.add_argument("--kind", default="build")
    p.add_argument("--produces", default=""); p.add_argument("--resources", default=""); p.add_argument("--mode", choices=["afk", "hitl"], default="afk")
    p.add_argument("--no-viz", action="store_true"); p.add_argument("--by", default=os.environ.get("USER", "agent")); p.set_defaults(f=cmd_add_node)
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = []
    if "--" in argv:                       # run <id> <node> [flags] -- <lệnh agent...>
        i = argv.index("--"); cmd = argv[i + 1:]; argv = argv[:i]
    a = ap.parse_args(argv); a.cmd = cmd
    if getattr(a, "node", None) == "-":
        a.node = None
    a.f(a)


if __name__ == "__main__":
    main()
