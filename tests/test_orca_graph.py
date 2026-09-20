"""test_orca_graph — cycle · ready-set · lease→unknown · op_key idempotent · gen stale bị chặn ·
kill -9 giữa lúc ghi không hỏng sổ (Reprise PRD bất biến 6) · audit bịa=0 · render graph-viz.py + graph-atlas.py."""
import importlib.util, json, os, signal, subprocess, sys, time
from pathlib import Path

import tempfile as _tf
os.environ.setdefault("ORCA_GRAPH_HOME", _tf.mkdtemp(prefix="og-test-home-"))   # KHÔNG đụng ~/.orca-graph thật (registry, daemon.lock)
os.environ.setdefault("ORCA_GRAPH_NO_DAEMON", "1")                              # test nào cần daemon thì tự bỏ biến này
os.environ.setdefault("ORCA_GRAPH_NO_ROOM", "1")     # test engine độc lập: không gọi cockpit overstack của máy thật mỗi lần emit
ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "engine/orca-graph.py"
_spec = importlib.util.spec_from_file_location("og", SCRIPT)
og = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(og)

PLAN = """# X
### Task 1: A
**Files:**
- Tạo: `a.py`
**Interfaces:**
- Consumes: —
- Produces (dùng bởi Task 2): `fa()`
**Verify:** `true`
### Task 2: B
**Files:**
- Sửa: `b.py`
**Interfaces:**
- Consumes: `fa()` (Task 1 Produces)
- Produces: `fb()`
**Depends:** Task 1
**Verify:** `false`
### Task 3: C
**Files:**
- Sửa: `a.py`
**Interfaces:**
- Consumes: —
- Produces: —
"""


QC_PLAN = """# Y
### Task 1: A
**Files:**
- Tạo: `a.py`
**Interfaces:**
- Consumes: —
- Produces: —
**Verify:** `true`
**QC:** `true`
### Task 2: B
**Files:**
- Sửa: `b.py`
**Interfaces:**
- Consumes: —
- Produces: —
**Verify:** `true`
**QC:** `false`
"""


def run(d, *args):
    return subprocess.run([sys.executable, str(SCRIPT), "--dir", str(d), *args], capture_output=True, text=True, cwd=ROOT)


def setup(tmp_path):
    p = tmp_path / "x-PLAN.md"; p.write_text(PLAN, encoding="utf-8")
    r = run(tmp_path, "build", str(p)); assert r.returncode == 0, r.stderr
    return "x"


def setup_qc(tmp_path):
    p = tmp_path / "y-PLAN.md"; p.write_text(QC_PLAN, encoding="utf-8")
    r = run(tmp_path, "build", str(p)); assert r.returncode == 0, r.stderr
    return "y"


def test_build_layers_and_conflict(tmp_path):
    gid = setup(tmp_path)
    g = json.loads((tmp_path / f"{gid}.graph.json").read_text())
    assert g["layers"] == [["t1", "t3"], ["t2"]]
    assert g["conflicts"] == [{"a": "t1", "b": "t3", "files": ["a.py"]}]
    assert {n["id"]: n["state"] for n in g["nodes"]} == {"t1": "ready", "t2": "proposed", "t3": "ready"}
    assert {n["id"]: n["deps_conf"] for n in g["nodes"]}["t2"] == "chắc"


def test_cycle_detected():
    nodes = {"a": {"deps": ["b"]}, "b": {"deps": ["a"]}}
    try:
        og.toposort(nodes); assert False
    except SystemExit as e:
        assert "CYCLE" in str(e)


def test_opkey_idempotent_and_gen_stale(tmp_path):
    gid = setup(tmp_path)
    assert run(tmp_path, "lock", gid, "t1", "--by", "x").returncode == 0
    assert run(tmp_path, "set", gid, "t1", "dispatched", "--op-key", "k1").returncode == 0
    r = run(tmp_path, "set", gid, "t1", "dispatched", "--op-key", "k1"); assert "no-op" in r.stdout
    r = run(tmp_path, "set", gid, "t1", "done", "--gen", "0"); assert "STALE" in r.stdout
    r = run(tmp_path, "set", gid, "t1", "done", "--gen", "1"); assert "→ done" in r.stdout
    g = json.loads((tmp_path / f"{gid}.graph.json").read_text())
    n = {x["id"]: x for x in g["nodes"]}
    assert n["t1"]["state"] == "done" and n["t1"]["attempts"] == 1 and n["t1"]["gen"] == 1
    assert n["t2"]["state"] == "ready"           # deps xong → mở khoá
    # verify fail → done_unverified, không phải done
    run(tmp_path, "lock", gid, "t2"); run(tmp_path, "set", gid, "t2", "dispatched", "--op-key", "k2")
    r = run(tmp_path, "set", gid, "t2", "done", "--gen", "1"); assert "done_unverified" in r.stdout


def test_cas_if_rev(tmp_path):
    gid = setup(tmp_path)
    r = run(tmp_path, "set", gid, "t3", "failed", "--if-rev", "9"); assert r.returncode != 0 and "CAS" in r.stderr


def test_lease_expired_to_unknown(tmp_path):
    gid = setup(tmp_path)
    run(tmp_path, "lock", gid, "t1", "--lease-sec", "1"); run(tmp_path, "set", gid, "t1", "dispatched", "--op-key", "k")
    time.sleep(1.2)
    r = run(tmp_path, "next", gid); assert "unknown" in r.stdout
    r = run(tmp_path, "reconcile", gid, "t1"); assert "→ done" in r.stdout   # verify=true


def test_kill9_mid_write_keeps_ledger(tmp_path):
    gid = setup(tmp_path)
    ev = tmp_path / f"{gid}.events.jsonl"
    child = subprocess.Popen([sys.executable, "-c", f"""
import json,time
f=open({str(ev)!r},'a')
for i in range(100000):
    f.write(json.dumps({{'ts':'t','node':'t3','from':'ready','to':'failed','op_key':'w%d'%i,'gen':0,'rev':i+1}})+'\\n'); f.flush()
"""])
    time.sleep(0.3); os.kill(child.pid, signal.SIGKILL); child.wait()
    r = run(tmp_path, "show", gid); assert r.returncode == 0, r.stderr   # dòng cuối cụt được bỏ, sổ vẫn đọc được
    assert "t3   failed" in r.stdout


def test_audit_fabricated_is_zero(tmp_path):
    gid = setup(tmp_path)
    run(tmp_path, "answer", gid, "t2", "--q", "deps", "--label", "chắc", "--evidence", "edge:t1->t2", "--text", "ok")
    run(tmp_path, "answer", gid, "t2", "--q", "why", "--label", "gợi-ý", "--evidence", "file:khong/co.py", "--text", "bịa")
    run(tmp_path, "answer", gid, "-", "--q", "related", "--label", "không-biết", "--text", "?")
    r = run(tmp_path, "answer", gid, "t2", "--q", "why", "--label", "chắc", "--text", "không nguồn"); assert r.returncode != 0
    r = run(tmp_path, "audit", gid)
    assert "BỊA" in r.stdout and "sau audit 1.3" in r.stdout


def test_render_viz_and_atlas(tmp_path):
    gid = setup(tmp_path)
    viz = ROOT / "engine/graph-viz.py"; atlas = ROOT / "engine/graph-atlas.py"
    r = subprocess.run([sys.executable, str(viz), str(tmp_path / f"{gid}.graph.json")], capture_output=True, text=True, cwd=ROOT); assert r.returncode == 0, r.stderr
    h = (tmp_path / f"{gid}.graph.html").read_text(encoding="utf-8")
    assert "theme-switch" in h and 'class="path"' in h and h.count('class="node"') == 3
    r = subprocess.run([sys.executable, str(atlas), str(tmp_path)], capture_output=True, text=True, cwd=ROOT); assert r.returncode == 0, r.stderr
    assert "Atlas" in (tmp_path / "atlas.html").read_text(encoding="utf-8")
    child = tmp_path / "c-PLAN.md"; child.write_text(PLAN, encoding="utf-8"); run(tmp_path, "build", str(child), "--parent", f"{gid}/t3")
    r = subprocess.run([sys.executable, str(atlas), str(tmp_path)], capture_output=True, text=True, cwd=ROOT); assert r.returncode == 0, r.stderr
    h = (tmp_path / "atlas.html").read_text(encoding="utf-8"); assert 'class="wire contain"' in h and "cấp 1" in h


def test_hierarchy_join(tmp_path):
    gid = setup(tmp_path)                                   # graph mẹ x: t1,t2,t3
    child = tmp_path / "c-PLAN.md"; child.write_text(PLAN.replace("Task 3", "Task 9"), encoding="utf-8")
    r = run(tmp_path, "build", str(child), "--parent", f"{gid}/t3"); assert r.returncode == 0, r.stderr
    g = json.loads((tmp_path / "c.graph.json").read_text()); assert g["parent"] == {"graph": gid, "node": "t3"} and g["depth"] == 1
    r = run(tmp_path, "show", gid); assert "child=c" in r.stdout
    for n in ("t1", "t2", "t9"):                            # xong hết con → mẹ t3 join
        run(tmp_path, "lock", "c", n); run(tmp_path, "set", "c", n, "dispatched", "--op-key", "k"+n); run(tmp_path, "set", "c", n, "done_user_reported", "--op-key", "d"+n)
    r = run(tmp_path, "show", gid); assert "t3   done_unverified" in r.stdout, r.stdout


def test_cross_graph_cycle_path(tmp_path):
    a = tmp_path / "a-PLAN.md"; a.write_text(PLAN.replace("**Depends:** Task 1", "**Depends:** Task 1, b/t3"), encoding="utf-8")
    b = tmp_path / "b-PLAN.md"; b.write_text(PLAN.replace("- Produces: —\n", "- Produces: —\n**Depends:** a/t2\n"), encoding="utf-8")
    assert run(tmp_path, "build", str(a)).returncode == 0 and run(tmp_path, "build", str(b)).returncode == 0
    r = run(tmp_path, "check-cycles"); assert r.returncode == 2 and "a/t2 → b/t3 → a/t2" in r.stdout, r.stdout   # ĐƯỜNG cycle cụ thể
    b.write_text(PLAN, encoding="utf-8"); run(tmp_path, "build", str(b))
    assert run(tmp_path, "check-cycles").returncode == 0
    g = json.loads((tmp_path / "a.graph.json").read_text()); assert {n["id"]: n["state"] for n in g["nodes"]}["t2"] == "proposed"  # chờ b/t3
    for n in ("t1", "t3"): run(tmp_path, "lock", "b", n); run(tmp_path, "set", "b", n, "done_user_reported", "--op-key", "b"+n)
    run(tmp_path, "lock", "a", "t1"); run(tmp_path, "set", "a", "t1", "done_user_reported", "--op-key", "a1")
    r = run(tmp_path, "show", "a"); assert "t2   ready" in r.stdout, r.stdout                 # dep ngoài đã thoả


def test_lint_leaf_contract(tmp_path):
    gid = setup(tmp_path)
    r = run(tmp_path, "lint", gid); assert r.returncode == 0
    assert "t3" in r.stdout and "verify" in r.stdout and "produces" in r.stdout   # t3 thiếu verify + produces
    assert "2/3 leaf đủ hợp đồng" in r.stdout, r.stdout
    r = run(tmp_path, "build", str(tmp_path / "x-PLAN.md"), "--strict"); assert r.returncode == 2


def test_plan_version_supersede(tmp_path):
    gid = setup(tmp_path); p = tmp_path / "x-PLAN.md"
    run(tmp_path, "lock", gid, "t1"); run(tmp_path, "set", gid, "t1", "dispatched", "--op-key", "k1"); run(tmp_path, "set", gid, "t1", "done", "--gen", "1", "--op-key", "d1")
    p.write_text(PLAN.replace("Task 1: A", "Task 1: A đổi tên").split("### Task 3")[0], encoding="utf-8")
    r = run(tmp_path, "build", str(p)); assert "plan_version 1 → 2" in r.stdout, r.stdout
    g = json.loads((tmp_path / f"{gid}.graph.json").read_text())
    assert g["plan_version"] == 2 and [n["id"] for n in g["superseded"]] == ["t3"]
    n = {x["id"]: x for x in g["nodes"]}; assert n["t1"]["state"] == "done" and n["t1"]["fresh"] == "stale", n["t1"]
    r = run(tmp_path, "set", gid, "t2", "done", "--plan-version", "1"); assert "STALE" in r.stdout


def test_control_and_max_parallel(tmp_path):
    gid = setup(tmp_path)
    run(tmp_path, "build", str(tmp_path / "x-PLAN.md"), "--max-parallel", "1")
    assert run(tmp_path, "lock", gid, "t1").returncode == 0
    r = run(tmp_path, "lock", gid, "t3"); assert r.returncode != 0 and "max_parallel" in r.stderr
    r = run(tmp_path, "control", gid, "pause"); assert "pause_requested" in r.stdout      # còn t1 locked → chưa paused
    r = run(tmp_path, "next", gid); assert "không cấp node mới" in r.stdout
    run(tmp_path, "set", gid, "t1", "failed", "--op-key", "f1")
    r = run(tmp_path, "control", gid, "status"); assert "control=paused" in r.stdout, r.stdout   # hết node đang chạy → paused
    run(tmp_path, "control", gid, "resume"); r = run(tmp_path, "next", gid); assert "t3" in r.stdout


def test_run_wrapper(tmp_path):
    gid = setup(tmp_path); home = tmp_path / "home"
    env = dict(os.environ, ORCA_GRAPH_HOME=str(home), ORCA_GRAPH_NO_DAEMON="1")
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", gid, "t1", "--hb", "0.2", "--", "sh", "-c", "sleep 0.5"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 0 and "→ done" in r.stdout, r.stdout + r.stderr          # verify=true
    reg = json.loads((home / "registry.json").read_text()); assert str(tmp_path.resolve()) not in reg["dirs"]   # hết node chạy → prune
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", gid, "t3", "--", "false"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode != 0 and "không có verify" in r.stderr                        # t3 không verify → từ chối headless
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", gid, "t3", "--allow-unverified", "--", "false"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert "→ failed" in r.stdout, r.stdout + r.stderr


def _mk_repo_and_graph(tmp_path):
    """git root = tmp_path; store orca-graph = tmp_path/store (KHÁC chỗ — đúng thực tế llmwiki/graph/ nested
    trong repo, không phải repo root, tránh nhầm bookkeeping của chính engine với file agent ghi)."""
    store = tmp_path / "store"; store.mkdir()
    p = tmp_path / "x-PLAN.md"; p.write_text(PLAN, encoding="utf-8")
    r = run(store, "build", str(p)); assert r.returncode == 0, r.stderr
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    return "x", store


def test_run_allowed_paths_warn(tmp_path):
    """GH#162 mặc định: file ghi ngoài `files` khai (t1 chỉ khai a.py) → CẢNH BÁO trong note, không xoá."""
    gid, store = _mk_repo_and_graph(tmp_path)
    (tmp_path / "a.py").write_text("x=1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    env = dict(os.environ, ORCA_GRAPH_HOME=str(tmp_path / "home"), ORCA_GRAPH_NO_DAEMON="1")
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(store), "run", gid, "t1", "--hb", "0.2", "--",
                         "sh", "-c", "echo z >> a.py && echo y > out-of-scope.py"],
                        capture_output=True, text=True, cwd=tmp_path, env=env)
    assert r.returncode == 0 and "→ done" in r.stdout, r.stdout + r.stderr
    events = [json.loads(ln) for ln in (store / f"{gid}.events.jsonl").read_text().splitlines()]
    last = [e for e in events if e["node"] == "t1" and e["to"] == "done"][-1]
    assert "ngoài phạm vi" in last["note"] and "out-of-scope.py" in last["note"], last
    assert (tmp_path / "out-of-scope.py").exists()   # mặc định chỉ cảnh báo, KHÔNG xoá


def test_run_allowed_paths_strict_revert(tmp_path):
    """GH#162 --strict: tracked ngoài phạm vi → git checkout phục hồi; untracked mới ngoài phạm vi → xoá."""
    gid, store = _mk_repo_and_graph(tmp_path)
    (tmp_path / "a.py").write_text("x=1\n"); (tmp_path / "b.py").write_text("y=1\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    env = dict(os.environ, ORCA_GRAPH_HOME=str(tmp_path / "home"), ORCA_GRAPH_NO_DAEMON="1")
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(store), "run", gid, "t1", "--strict", "--hb", "0.2", "--",
                         "sh", "-c", "echo z >> b.py && echo y > new-out.py"],
                        capture_output=True, text=True, cwd=tmp_path, env=env)
    assert r.returncode == 0 and "→ done" in r.stdout, r.stdout + r.stderr
    assert not (tmp_path / "new-out.py").exists()          # untracked mới ngoài phạm vi → xoá
    assert (tmp_path / "b.py").read_text() == "y=1\n"        # tracked ngoài phạm vi → checkout phục hồi HEAD


def test_qc_field_parsed_and_in_spec_hash(tmp_path):
    """GH#163: `**QC:**` parse đúng vào node['qc'], và nằm trong hợp đồng spec_hash (đổi qc → stale)."""
    gid = setup_qc(tmp_path)
    g = json.loads((tmp_path / f"{gid}.graph.json").read_text())
    nodes = {n["id"]: n for n in g["nodes"]}
    assert nodes["t1"]["qc"] == "true" and nodes["t2"]["qc"] == "false"
    h_before = nodes["t2"]["spec_hash"]
    p2 = tmp_path / "y-PLAN.md"; p2.write_text(QC_PLAN.replace("**QC:** `false`", "**QC:** `true`"), encoding="utf-8")
    r = run(tmp_path, "build", str(p2), "--id", gid); assert r.returncode == 0, r.stderr
    g2 = json.loads((tmp_path / f"{gid}.graph.json").read_text())
    assert {n["id"]: n for n in g2["nodes"]}["t2"]["spec_hash"] != h_before   # đổi lệnh QC → hợp đồng đổi


def test_qc_gate_blocks_done_via_run(tmp_path):
    """GH#163: verify xanh nhưng qc đỏ → `run` KHÔNG cho done, tự demote qua emit() (giống cơ chế verify hiện có)."""
    gid = setup_qc(tmp_path)
    env = dict(os.environ, ORCA_GRAPH_HOME=str(tmp_path / "home"), ORCA_GRAPH_NO_DAEMON="1")
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", gid, "t2", "--hb", "0.2", "--", "true"],
                        capture_output=True, text=True, cwd=ROOT, env=env)
    assert "→ done_unverified" in r.stdout, r.stdout + r.stderr
    events = [json.loads(ln) for ln in (tmp_path / f"{gid}.events.jsonl").read_text().splitlines()]
    last = [e for e in events if e["node"] == "t2"][-1]
    assert last["to"] == "done_unverified" and "qc rc=" in last["note"], last
    # t1 (qc=true) phải qua trót lọt bình thường — chứng minh gate không chặn nhầm ca hợp lệ
    r2 = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", gid, "t1", "--hb", "0.2", "--", "true"],
                         capture_output=True, text=True, cwd=ROOT, env=env)
    assert "→ done" in r2.stdout and "done_unverified" not in r2.stdout, r2.stdout + r2.stderr


def test_qc_gate_blocks_done_via_reconcile(tmp_path):
    """GH#163: reconcile tự chạy verify RỒI qc — qc fail thì kết luận `ready` (không phải done_unverified) và mở khoá."""
    gid = setup_qc(tmp_path)
    run(tmp_path, "lock", gid, "t2"); run(tmp_path, "set", gid, "t2", "dispatched", "--op-key", "k1")
    r = run(tmp_path, "reconcile", gid, "t2")
    assert "t2: dispatched → ready" in r.stdout, r.stdout + r.stderr
    events = [json.loads(ln) for ln in (tmp_path / f"{gid}.events.jsonl").read_text().splitlines()]
    assert "qc rc=1 FAIL" in events[-1]["note"], events[-1]
    g = json.loads((tmp_path / f"{gid}.graph.json").read_text())
    n = {x["id"]: x for x in g["nodes"]}["t2"]
    assert n["state"] == "ready"
    assert not (tmp_path / f"{gid}.locks" / "t2").exists()   # reconcile fail → mở khoá


def test_watch_once(tmp_path):
    gid = setup(tmp_path); home = tmp_path / "home"; home.mkdir()
    (home / "registry.json").write_text(json.dumps({"dirs": [str(tmp_path.resolve())]}))
    env = dict(os.environ, ORCA_GRAPH_HOME=str(home))
    run(tmp_path, "lock", gid, "t1", "--lease-sec", "1"); run(tmp_path, "set", gid, "t1", "dispatched", "--op-key", "k"); time.sleep(1.2)
    r = subprocess.run([sys.executable, str(SCRIPT), "watch", "--once"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 0 and "t1" in r.stdout and "reconcile" in r.stdout, r.stdout + r.stderr
    g = json.loads((tmp_path / f"{gid}.graph.json").read_text()); assert {n["id"]: n["state"] for n in g["nodes"]}["t1"] == "done"
    assert not (home / "daemon.lock").exists()


def test_concurrent_runs_same_dir(tmp_path):
    """Race 140926: 2 run cùng lúc trong 1 dir — cả hai phải có event dispatched, không wrapper nào chết vì tmp file chung."""
    gid = setup(tmp_path); env = dict(os.environ, ORCA_GRAPH_HOME=str(tmp_path / "home"), ORCA_GRAPH_NO_DAEMON="1")
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", gid, n, "--allow-unverified", "--hb", "0.2", "--", "sh", "-c", "sleep 0.6"],
                              cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for n in ("t1", "t3")]
    outs = [p.communicate() for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    ev = [json.loads(l) for l in (tmp_path / f"{gid}.events.jsonl").read_text().splitlines() if l.strip()]
    assert {e["node"] for e in ev if e.get("to") == "dispatched"} == {"t1", "t3"}
    assert not list(tmp_path.glob("*.tmp"))


if __name__ == "__main__":
    import tempfile
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d)) if fn.__code__.co_argcount else fn()
            print("ok", name)


def test_verify_runs_under_bash_gh166(tmp_path):
    """GH#166: `**Verify:**` dùng cú pháp chỉ-bash (process substitution) phải chạy được — trước đây shell=True = /bin/sh → rc=2."""
    p = tmp_path / "z-PLAN.md"
    p.write_text("# Z\n### Task 1: A\n**Files:**\n- Tạo: `a.py`\n**Verify:** `cat < <(echo ok) >/dev/null`\n", encoding="utf-8")
    assert run(tmp_path, "build", str(p)).returncode == 0
    env = dict(os.environ, ORCA_GRAPH_HOME=str(tmp_path / "home"), ORCA_GRAPH_NO_DAEMON="1")
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", "z", "t1", "--hb", "0.2", "--", "true"],
                       capture_output=True, text=True, cwd=ROOT, env=env)
    assert "→ done_unverified" not in r.stdout and "→ done" in r.stdout, r.stdout + r.stderr


# ---------- v3 · PRD v1.1 §23: edge có lý do + audit-edges ----------
EDGE_PLAN = """# E
### Task 1: đọc A
**Files:**
- Tạo: `a.txt`
**Interfaces:**
- Consumes: —
- Produces: `a`
**Verify:** `true`
### Task 2: đọc B
**Files:**
- Tạo: `b.txt`
**Interfaces:**
- Consumes: —
- Produces: `b`
**Depends:** Task 1 (preference)
**Verify:** `true`
### Task 3: đọc C
**Files:**
- Tạo: `c.txt`
**Interfaces:**
- Consumes: —
- Produces: `c`
**Depends:** Task 2 (scheduling_preference)
**Verify:** `true`
### Task 4: duyệt rồi publish
**Files:**
- Tạo: `out.txt`
**Interfaces:**
- Consumes: —
- Produces: `out`
**Depends:** Task 3 (control), Task 1
**Verify:** `true`
"""


def setup_edges(tmp_path, text=EDGE_PLAN):
    p = tmp_path / "e-PLAN.md"; p.write_text(text, encoding="utf-8")
    r = run(tmp_path, "build", str(p)); assert r.returncode == 0, r.stdout + r.stderr
    return "e"


def audit_json(tmp_path, gid):
    r = run(tmp_path, "audit-edges", gid, "--json"); assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_edge_reason_parse_keeps_deps_and_spec_hash(tmp_path):
    gid = setup_edges(tmp_path)
    nodes = {n["id"]: n for n in json.loads((tmp_path / f"{gid}.graph.json").read_text())["nodes"]}
    assert nodes["t2"]["deps"] == ["t1"] and nodes["t2"]["dep_reasons"] == {"t1": "preference"}
    assert nodes["t3"]["dep_reasons"] == {"t2": "preference"}          # alias scheduling_preference
    assert nodes["t4"]["deps"] == ["t3", "t1"] and nodes["t4"]["dep_reasons"] == {"t3": "control"}
    # gắn lý do cho cạnh KHÔNG đổi hợp đồng node (spec_hash) → node đã xong không thành stale
    bare = og.parse_plan(EDGE_PLAN.replace(" (preference)", "").replace(" (scheduling_preference)", "").replace(" (control)", ""))
    assert {t["id"]: og.spec_hash(t) for t in bare} == {i: n["spec_hash"] for i, n in nodes.items()}


def test_edge_unknown_reason_rejected(tmp_path):
    p = tmp_path / "e-PLAN.md"; p.write_text(EDGE_PLAN.replace("(control)", "(vibes)"), encoding="utf-8")
    r = run(tmp_path, "build", str(p))
    assert r.returncode != 0 and "reason_class" in r.stderr


def test_VT01_edge_audit_proposes_removing_prompt_order_only(tmp_path):
    """VT-01 / QA-GX-02: 3 read độc lập bị xếp chuỗi chỉ vì thứ tự viết → đề xuất mở rộng, KHÔNG đổi plan."""
    gid = setup_edges(tmp_path)
    before = (tmp_path / f"{gid}.graph.json").read_text(), (tmp_path / "e-PLAN.md").read_text()
    r = audit_json(tmp_path, gid)
    assert r["diff"]["remove_preference_edges"] == ["t1->t2", "t2->t3"]
    assert len(r["critical_path"]["before"]) == 4 and len(r["critical_path"]["after"]) == 2
    assert r["frontier"]["after"] == ["t1", "t2", "t3"]
    assert "unit-weight" in r["critical_path"]["unit"]                    # không giả làm thời lượng
    assert before == ((tmp_path / f"{gid}.graph.json").read_text(), (tmp_path / "e-PLAN.md").read_text())   # dry-run


def test_VT02_edge_audit_keeps_control_gate_without_payload(tmp_path):
    """VT-02 / QA-GX-01: cạnh phê duyệt không mang artifact vẫn là cạnh cứng; cạnh thiếu lý do báo UNJUSTIFIED và GIỮ."""
    gid = setup_edges(tmp_path)
    f = {x["edge"]: x for x in audit_json(tmp_path, gid)["findings"]}
    assert "t3->t4" not in f                                              # control: hợp lệ, không finding
    assert f["t1->t4"]["code"] == "EDGE_UNJUSTIFIED" and f["t1->t4"]["action"] == "keep"
    assert run(tmp_path, "audit-edges", gid, "--strict").returncode == 2


def test_edge_audit_preference_with_hidden_constraint_is_kept(tmp_path):
    """§23.3 bước 3: cạnh preference nhưng hai node cùng ghi một file → không đề xuất bỏ."""
    gid = setup_edges(tmp_path, EDGE_PLAN.replace("- Tạo: `b.txt`", "- Tạo: `a.txt`"))
    r = audit_json(tmp_path, gid)
    f = {x["edge"]: x for x in r["findings"]}
    assert f["t1->t2"]["code"] == "PREFERENCE_HAS_HIDDEN_CONSTRAINT" and "t1->t2" not in r["diff"]["remove_preference_edges"]


# ---------- v3 · PRD v1.1 §23.4: resource claims ----------
def res_plan(claims):
    return "# R\n" + "".join(f"""### Task {i}: việc {i}
**Files:**
- Tạo: `wt{i}/f.py`
**Interfaces:**
- Consumes: —
- Produces: `o{i}`
**Resources:** {c}
**Verify:** `true`
""" for i, c in enumerate(claims, 1))


def setup_res(tmp_path, claims, name="r"):
    p = tmp_path / f"{name}-PLAN.md"; p.write_text(res_plan(claims), encoding="utf-8")
    r = run(tmp_path, "build", str(p)); assert r.returncode == 0, r.stdout + r.stderr
    return name, r.stdout


def test_VT04_resource_exclusive_serializes_independent_worktrees(tmp_path):
    """VT-04 / QA-GX-03: file tách nhau (không xung đột ghi, không cạnh DAG) nhưng chung migration namespace → tuần tự lúc lock."""
    gid, out = setup_res(tmp_path, ["db-migration(exclusive)", "./DB-Migration"])      # alias khác chữ vẫn là MỘT key
    g = json.loads((tmp_path / f"{gid}.graph.json").read_text())
    assert g["layers"] == [["t1", "t2"]] and g["conflicts"] == [] and g["resource_conflicts"][0]["keys"] == ["db-migration"]
    assert "KHÔNG cần thêm Depends" in out
    assert run(tmp_path, "lock", gid, "t1").returncode == 0
    r = run(tmp_path, "lock", gid, "t2")
    assert r.returncode != 0 and "RESOURCE" in r.stderr and "db-migration" in r.stderr and f"{gid}/t1" in r.stderr
    assert not (tmp_path / f"{gid}.locks" / "t2").exists()                           # không giữ gì khi bị từ chối
    run(tmp_path, "set", gid, "t1", "dispatched"); run(tmp_path, "set", gid, "t1", "done", "--gen", "1")
    assert run(tmp_path, "lock", gid, "t2").returncode == 0


def test_VT03_resource_shared_quota_queues_the_excess(tmp_path):
    """VT-03 / QA-GX-03: shared read chạy tới quota rồi phần thừa phải chờ; shared+shared không chặn nhau."""
    gid, _ = setup_res(tmp_path, ["api-x(capacity:2)", "api-x(capacity:2)", "api-x(capacity:2)", "ds(shared)", "ds(shared)"])
    for n in ("t1", "t2", "t4", "t5"):
        assert run(tmp_path, "lock", gid, n).returncode == 0
    st = {n["id"]: n["state"] for n in og.Store(tmp_path, gid).load()["nodes"]}
    # max_parallel mặc định 4: t1 t2 (capacity) + t4 t5 (shared) đều vào được
    assert [st[x] for x in ("t1", "t2", "t4", "t5")] == ["locked"] * 4
    run(tmp_path, "set", gid, "t4", "dispatched"); run(tmp_path, "set", gid, "t4", "done", "--gen", "1")
    r = run(tmp_path, "lock", gid, "t3")
    assert r.returncode != 0 and "capacity 2/2" in r.stderr


def test_resource_claim_blocks_across_graphs_in_same_store(tmp_path):
    a, _ = setup_res(tmp_path, ["integration-head"], "ga")
    b, _ = setup_res(tmp_path, ["integration-head"], "gb")
    assert run(tmp_path, "lock", a, "t1").returncode == 0
    r = run(tmp_path, "lock", b, "t1")
    assert r.returncode != 0 and "ga/t1" in r.stderr


def test_resource_claims_not_in_spec_hash(tmp_path):
    with_c = og.parse_plan(res_plan(["k(exclusive)"]))[0]; without = og.parse_plan(res_plan(["—"]))[0]
    assert with_c["resources"] == [{"key": "k", "mode": "exclusive"}] and "resources" not in without
    assert og.spec_hash(with_c) == og.spec_hash(without)


# ---------- v3 · PRD v1.1 §28.4: lý do chờ ----------
WAIT_PLAN = res_plan(["k(exclusive)", "k(exclusive)", "—", "—"]) + """### Task 5: publish
**Files:**
- Tạo: `out.txt`
**Interfaces:**
- Consumes: —
- Produces: `out`
**Depends:** Task 3 (acceptance)
**Verify:** `true`
### Task 6: cần người
**Files:**
- Tạo: `h.txt`
**Interfaces:**
- Consumes: —
- Produces: `h`
**Mode:** HITL
**Depends:** Task 1 (data)
**Verify:** `true`
"""


def test_waiting_separates_data_gate_resource_queue_control(tmp_path):
    """QA-GX-15: resource wait và data wait hiển thị KHÁC nhau; ready ≠ admitted."""
    p = tmp_path / "w-PLAN.md"; p.write_text(WAIT_PLAN, encoding="utf-8")
    assert run(tmp_path, "build", str(p), "--max-parallel", "2").returncode == 0
    assert run(tmp_path, "lock", "w", "t1").returncode == 0
    w = og.wait_reasons(og.Store(tmp_path, "w").load(), tmp_path)
    assert w["t2"]["reason"] == "resource" and "w/t1" in w["t2"]["detail"]
    assert w["t3"]["reason"] == "none"
    assert w["t5"]["reason"] == "gate" and w["t6"]["reason"] == "data"
    assert run(tmp_path, "lock", "w", "t3").returncode == 0
    w = og.wait_reasons(og.Store(tmp_path, "w").load(), tmp_path)
    assert w["t4"]["reason"] == "queue" and w["t2"]["reason"] == "resource"          # resource thắng queue: nói đúng cái đang chặn
    out = run(tmp_path, "next", "w").stdout
    assert "t4 ready nhưng CHƯA lock được — chờ queue" in out
    out = run(tmp_path, "ask", "w", "waiting").stdout
    assert "0/6 xong" in out and "resource=1" in out
    run(tmp_path, "control", "w", "pause")
    assert {r["reason"] for r in og.wait_reasons(og.Store(tmp_path, "w").load(), tmp_path).values()} == {"control"}


# ---------- v3: add-node — user yêu cầu thêm node giữa chừng ----------
def test_add_node_appends_to_plan_rebuilds_and_keeps_history(tmp_path):
    p = tmp_path / "x-PLAN.md"; p.write_text(PLAN + "\n## Origin\n\n- test\n", encoding="utf-8")
    assert run(tmp_path, "build", str(p)).returncode == 0
    run(tmp_path, "lock", "x", "t1"); run(tmp_path, "set", "x", "t1", "dispatched"); run(tmp_path, "set", "x", "t1", "done", "--gen", "1")
    r = run(tmp_path, "add-node", "x", "--title", "Viết changelog", "--depends", "t1:data", "--files", "CHANGELOG.md",
            "--verify", "true", "--kind", "docs", "--resources", "repo-head(exclusive)", "--no-viz")
    assert r.returncode == 0 and "+ t4" in r.stdout, r.stdout + r.stderr
    text = p.read_text()
    assert text.index("### Task 4: Viết changelog") < text.index("## Origin")            # chèn TRƯỚC mục đuôi, không lọt ra ngoài vùng task
    assert "**Depends:** Task 1 (data)" in text
    g = og.Store(tmp_path, "x").load()
    n = {x["id"]: x for x in g["nodes"]}
    assert g["plan_version"] == 2 and n["t4"]["state"] == "ready" and n["t4"]["kind"] == "docs"
    assert n["t4"]["dep_reasons"] == {"t1": "data"} and n["t4"]["resources"] == [{"key": "repo-head", "mode": "exclusive"}]
    assert n["t1"]["state"] == "done" and n["t1"]["fresh"] == "current"                  # lịch sử giữ nguyên, node cũ không stale
    assert any(e.get("kind") == "node.added" and e["node"] == "t4" for e in og.read_jsonl(tmp_path / "x.events.jsonl"))


def test_add_node_blocks_inserts_in_the_middle(tmp_path):
    gid = setup(tmp_path)
    r = run(tmp_path, "add-node", gid, "--title", "Kiểm schema", "--depends", "t1", "--blocks", "t2,t3", "--files", "s.py", "--verify", "true", "--no-viz")
    assert r.returncode == 0, r.stderr
    n = {x["id"]: x for x in og.Store(tmp_path, gid).load()["nodes"]}
    assert n["t2"]["deps"] == ["t1", "t4"]                 # dòng Depends có sẵn được nối thêm
    assert n["t3"]["deps"] == ["t4"] and n["t3"]["deps_conf"] == "chắc"   # task chưa có dòng Depends → được thêm mới


def test_add_node_rejection_leaves_plan_untouched(tmp_path):
    gid = setup(tmp_path); p = tmp_path / "x-PLAN.md"; before = p.read_text()
    r = run(tmp_path, "add-node", gid, "--title", "Vòng", "--depends", "t2", "--blocks", "t1", "--no-viz")   # t1→t2→mới→t1
    assert r.returncode != 0 and "CYCLE" in r.stderr and p.read_text() == before
    assert run(tmp_path, "add-node", gid, "--title", "X", "--depends", "t9", "--no-viz").returncode != 0
    assert run(tmp_path, "add-node", gid, "--title", "X", "--resources", "k(capacity)", "--no-viz").returncode != 0
    assert p.read_text() == before and og.Store(tmp_path, gid).load()["plan_version"] == 1


def test_add_node_rebuild_keeps_parent_link(tmp_path):
    gid = setup(tmp_path)
    c = tmp_path / "kid-PLAN.md"; c.write_text(PLAN, encoding="utf-8")
    assert run(tmp_path, "build", str(c), "--parent", f"{gid}/t1").returncode == 0
    assert run(tmp_path, "add-node", "kid", "--title", "Thêm", "--files", "z.py", "--verify", "true", "--no-viz").returncode == 0
    g = og.Store(tmp_path, "kid").load()
    assert g["parent"] == {"graph": gid, "node": "t1"} and g["depth"] == 1


# ---------- v3 · PRD v1.1 §24.1/§25.5/§26.4/§27.2: identity · dry streak · cost envelope ----------
def _verdicts(tmp_path, rows):
    p = tmp_path / "v.jsonl"; p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"); return str(p)


def test_VT09_duplicate_item_with_matching_count_is_not_complete(tmp_path):
    """VT-09 / QA-GX-04: expected A/B/C, actual A/A/B — đủ 3 dòng nhưng thiếu C."""
    r = run(tmp_path, "reconcile-items", "--expected", "A,B,C", "--verdicts",
            _verdicts(tmp_path, [{"item_id": "A", "verdict": "supported"}, {"item_id": "A", "verdict": "supported"}, {"item_id": "B", "verdict": "supported"}]))
    assert r.returncode == 2 and json.loads(r.stdout) == {"protocol_error": "DUPLICATE_VERDICT"}
    for rows, code in (([{"item_id": "Z", "verdict": "supported"}], "UNEXPECTED_ITEM_ID"), ([{"item_id": "A", "verdict": "pass"}], "INVALID_VERDICT"), (["A"], "MALFORMED_VERDICT")):
        try:
            og.reconcile_verdicts(["A", "B"], rows); assert False
        except ValueError as e:
            assert str(e) == code
    try:
        og.reconcile_verdicts(["A", "A"], []); assert False
    except ValueError as e:
        assert str(e) == "DUPLICATE_MANIFEST_ID"


def test_VT10_null_worker_response_stays_a_typed_missing_outcome(tmp_path):
    """VT-10: null/timeout không bị filter mất — vẫn hiện ID ở missing + errored, và KHÔNG bị tính là refuted."""
    r = run(tmp_path, "reconcile-items", "--expected", "A,B,C", "--verdicts",
            _verdicts(tmp_path, [{"item_id": "A", "verdict": "supported"}, {"item_id": "B", "verdict": None}, {"item_id": "C", "status": "timeout", "verdict": "refuted"}]))
    out = json.loads(r.stdout)
    assert r.returncode == 2 and out["missing_ids"] == ["B", "C"] and out["errored_ids"] == ["B", "C"] and out["refuted_ids"] == []
    assert out["counts"] == {"expected": 3, "received": 1}


def test_VT17_verdict_order_differs_from_finding_order(tmp_path):
    """VT-17 / QA-GX-06: verdict tới theo thứ tự C,A,B — ghép theo ID chứ không theo vị trí."""
    rows = [{"item_id": "C", "verdict": "supported"}, {"item_id": "A", "verdict": "refuted"}, {"item_id": "B", "verdict": "supported"}]
    out = og.reconcile_verdicts(["A", "B", "C"], rows)
    assert out["supported_ids"] == ["B", "C"] and out["refuted_ids"] == ["A"] and out["missing_ids"] == [] and out["all_results_received"]
    assert og.reconcile_verdicts(["A", "B", "C"], [rows[0], rows[1]])["missing_ids"] == ["B"]
    assert run(tmp_path, "reconcile-items", "--expected", "A,B,C", "--verdicts", _verdicts(tmp_path, rows)).returncode == 0
    assert og.reconcile_verdicts([], [])["all_results_received"] is True       # manifest rỗng: caller phải có policy, helper không tự suy "sạch"


def test_VT19_finder_error_round_resets_dry_streak(tmp_path):
    """VT-19 / QA-GX-10: (đủ,0) → (thiếu,0) → (đủ,0) cho 1 → 0 → 1 — chưa hội tụ."""
    s = 0; seq = []
    for complete, new in ((True, 0), (False, 0), (True, 0)):
        s = og.next_dry_streak(s, round_complete=complete, new_count=new); seq.append(s)
    assert seq == [1, 0, 1]
    assert og.next_dry_streak(1, round_complete=True, new_count=1) == 0          # finding mới (kể cả sau đó bị bác) reset streak
    for bad in ((-1, True, 0), (0, 1, 0), (0, True, -1), (True, True, 0)):
        try:
            og.next_dry_streak(bad[0], round_complete=bad[1], new_count=bad[2]); assert False
        except ValueError:
            pass
    out = json.loads(run(tmp_path, "dry-streak", "--prev", "1", "--complete", "1", "--new", "0").stdout)
    assert out["dry_streak"] == 2 and out["converged_heuristic"] is True and "KHÔNG chứng minh" in out["note"]


def test_VT23_cost_envelope_counts_every_call_not_agents(tmp_path):
    """VT-23 / QA-GX-12: 20 item, 1 reviewer = 42 call; 3 reviewer = 82 — trước retry."""
    plan = "# C\n" + "".join(f"### Task {i}: item {i}\n**Files:**\n- Tạo: `f{i}`\n**Interfaces:**\n- Consumes: —\n- Produces: `o`\n**Verify:** `true`\n**QC:** `true`\n" for i in range(1, 21))
    p = tmp_path / "c-PLAN.md"; p.write_text(plan, encoding="utf-8"); assert run(tmp_path, "build", str(p)).returncode == 0
    one = json.loads(run(tmp_path, "cost-envelope", "c").stdout)
    three = json.loads(run(tmp_path, "cost-envelope", "c", "--reviewers", "3").stdout)
    assert one["min_calls"] == 42 and three["min_calls"] == 82
    assert one["max_calls"] == 1 + 3 * 40 + 1 and one["expected_calls"].startswith("unknown")
    assert one["deterministic_checks"] == 20 and "không ghi tổng cost = 0" in one["deterministic_note"]


def test_VT28_sample_coverage_is_not_full_audit(tmp_path):
    """VT-28 / QA-GX-04: chọn 20 trong 100 đã biết, stage 20/20 xong → vẫn báo 20/100; không biết toàn tập → unknown, không 100%."""
    ids = [f"f{i}" for i in range(20)]
    v = _verdicts(tmp_path, [{"item_id": i, "verdict": "supported"} for i in ids])
    out = json.loads(run(tmp_path, "reconcile-items", "--expected", ",".join(ids), "--verdicts", v, "--universe", "100").stdout)
    assert out["coverage"] == {"completed/selected": "20/20", "selected/known_universe": "20/100"}
    out = json.loads(run(tmp_path, "reconcile-items", "--expected", ",".join(ids), "--verdicts", v).stdout)
    assert out["coverage"]["selected/known_universe"] == "20/unknown"


# ---------- v3 · hành vi sẵn có của engine, gắn mã VT để ma trận eval trỏ tới ----------
PIPE_PLAN = """# P
### Task 1: collect A (chậm)
**Files:**
- Tạo: `a`
**Verify:** `true`
### Task 2: collect B (nhanh)
**Files:**
- Tạo: `b`
**Verify:** `true`
### Task 3: verify A
**Files:**
- Tạo: `va`
**Depends:** Task 1 (data)
**Verify:** `true`
### Task 4: verify B
**Files:**
- Tạo: `vb`
**Depends:** Task 2 (data)
**Verify:** `true`
### Task 5: xếp hạng toàn tập
**Files:**
- Tạo: `rank`
**Depends:** Task 3 (data), Task 4 (data)
**Verify:** `true`
"""


def _finish(tmp_path, gid, n):
    assert run(tmp_path, "lock", gid, n).returncode == 0
    run(tmp_path, "set", gid, n, "dispatched"); run(tmp_path, "set", gid, n, "done", "--gen", "1")


def test_VT05_VT06_wave_is_not_a_barrier_but_full_set_join_waits(tmp_path):
    """VT-05: B xong trước A → verify-B chạy NGAY dù A (cùng lớp với B) chưa xong — lớp topo là hình chiếu, không phải rào.
    VT-06: node cần TOÀN TẬP (rank) chỉ ready khi mọi upstream xong."""
    p = tmp_path / "p-PLAN.md"; p.write_text(PIPE_PLAN, encoding="utf-8"); assert run(tmp_path, "build", str(p)).returncode == 0
    assert run(tmp_path, "lock", "p", "t1").returncode == 0                       # A đang chạy, chưa xong
    _finish(tmp_path, "p", "t2")
    st = lambda: {n["id"]: n["state"] for n in og.Store(tmp_path, "p").load()["nodes"]}
    assert st()["t4"] == "ready" and st()["t3"] == "proposed"
    _finish(tmp_path, "p", "t4")
    assert st()["t5"] == "proposed"                                               # thiếu verify-A → chưa đủ tập
    assert og.wait_reasons(og.Store(tmp_path, "p").load(), tmp_path)["t5"] == {"reason": "data", "detail": "chờ upstream ['t3']"}


def test_VT15_failed_check_beats_a_claimed_pass(tmp_path):
    """VT-15 / QA-GX-08: worker tự báo `done` nhưng verify rc≠0 → done_unverified; downstream không mở."""
    gid = setup(tmp_path)
    _finish(tmp_path, gid, "t1")
    run(tmp_path, "lock", gid, "t2"); run(tmp_path, "set", gid, "t2", "dispatched")
    r = run(tmp_path, "set", gid, "t2", "done", "--gen", "1", "--note", "worker: ALL PASS, 3/3 reviewers agree")
    n = {x["id"]: x for x in og.Store(tmp_path, gid).load()["nodes"]}["t2"]
    assert n["state"] == "done_unverified" and n["verified"] == "unverified"


def test_VT26_VT27_stale_results_never_join_the_new_plan(tmp_path):
    """VT-27: kết quả tới muộn của plan_version cũ → STALE, không publish. VT-26: hợp đồng node đổi sau khi xong → biên lai cũ không tự mang sang (fresh=stale)."""
    gid = setup(tmp_path); p = tmp_path / "x-PLAN.md"
    run(tmp_path, "lock", gid, "t1"); run(tmp_path, "set", gid, "t1", "dispatched")
    p.write_text(PLAN.replace("### Task 3: C", "### Task 3: C đổi tên"), encoding="utf-8"); run(tmp_path, "build", str(p))
    r = run(tmp_path, "set", gid, "t1", "done", "--gen", "1", "--plan-version", "1")
    assert "STALE" in r.stdout and {x["id"]: x for x in og.Store(tmp_path, gid).load()["nodes"]}["t1"]["state"] == "dispatched"
    run(tmp_path, "set", gid, "t1", "done", "--gen", "1", "--plan-version", "2")
    p.write_text(p.read_text().replace("- Tạo: `a.py`", "- Tạo: `a2.py`"), encoding="utf-8"); run(tmp_path, "build", str(p))
    assert {x["id"]: x for x in og.Store(tmp_path, gid).load()["nodes"]}["t1"]["fresh"] == "stale"


# ---------- v3 · hồi quy từ review độc lập 20/09/2026 (mỗi test = một lỗi đã tái hiện được) ----------
def _task(i, extra="", files=None):
    return f"### Task {i}: việc {i}\n**Files:**\n- Tạo: `{files or f'f{i}.py'}`\n**Interfaces:**\n- Consumes: —\n- Produces: `o{i}`\n{extra}**Verify:** `true`\n"


def _build(tmp_path, text, name="rv"):
    p = tmp_path / f"{name}-PLAN.md"; p.write_text(text, encoding="utf-8")
    return run(tmp_path, "build", str(p)), p


def test_review_C3_parser_refuses_to_swallow_unresolvable_depends(tmp_path):
    """Cạnh bị nuốt im lặng = node ready sớm mà audit-edges không thấy. Token không hiểu phải là LỖI TO."""
    for bad in ("Task 1 (data) — vì cần schema", "Task 1 (data; schema)", "Task 1 và Task 2", "Task 9", "Task 3", "<img src=x>/t1 (control)", "Task 1 (data), Task 1 (control)"):
        r, _ = _build(tmp_path, "# R\n" + _task(1) + _task(2) + _task(3, f"**Depends:** {bad}\n"))
        assert r.returncode != 0 and "Depends" in r.stderr, (bad, r.stdout, r.stderr)
    ok = {"Task 1 (effect-order)": (["t1"], {"t1": "effect_order"}), "`Task 1 (data)`, t2 (contract)": (["t1", "t2"], {"t1": "data", "t2": "contract"}),
          "Task 1; Task 2 (DATA)": (["t1", "t2"], {"t2": "data"}), "Task 1, Task 1": (["t1"], {}), "other-g/t3 (control)": (["other-g/t3"], {"other-g/t3": "control"}), "—": ([], {})}
    for dep, (deps, reasons) in ok.items():
        n = og.parse_plan("# R\n" + _task(1) + _task(2) + _task(3, f"**Depends:** {dep}\n"))[2]
        assert n["deps"] == deps and n.get("dep_reasons", {}) == reasons, (dep, n["deps"], n.get("dep_reasons"))
    r, _ = _build(tmp_path, "# R\n" + _task(1, "**Kind:** <img src=x onerror=alert(2)>\n"))
    assert r.returncode != 0 and "Kind" in r.stderr


FENCE_PLAN = "# F\n" + _task(1).replace("**Verify:**", "```md\n### Task 9: mẫu trong fence\n**Depends:** Task 0\n## Không phải mục thật\n```\n**Verify:**") + _task(2) + \
             "\n```md\n### Task 7: mẫu sau task cuối\n```\n\n## Origin\n\n- x\n"


def test_review_C1_V7_add_node_sees_plan_like_the_parser_fences_ignored(tmp_path):
    r, p = _build(tmp_path, FENCE_PLAN, "fz"); assert r.returncode == 0, r.stderr
    r = run(tmp_path, "add-node", "fz", "--title", "Chèn", "--blocks", "t1", "--files", "z.py", "--verify", "true", "--no-viz")
    assert r.returncode == 0 and "+ t3" in r.stdout, r.stdout + r.stderr                 # id kế tiếp là t3, không phải t10 (Task 9 trong fence là mẫu)
    text = p.read_text()
    assert "**Depends:** Task 0\n" in text                                                # dòng mẫu trong fence KHÔNG bị sửa
    assert text.index("### Task 3: Chèn") < text.index("## Origin")                       # không bị đẩy ra sau ## Origin
    n = {x["id"]: x for x in og.Store(tmp_path, "fz").load()["nodes"]}
    assert n["t1"]["deps"] == ["t3"] and n["t1"]["state"] == "proposed"                   # chèn-vào-giữa có tác dụng THẬT


def test_review_C2_concurrent_add_node_does_not_lose_a_task(tmp_path):
    r, p = _build(tmp_path, "# C\n" + _task(1), "cc"); assert r.returncode == 0
    procs = [subprocess.Popen([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "add-node", "cc", "--title", f"song song {k}", "--files", f"s{k}.py", "--verify", "true", "--no-viz"],
                              cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for k in range(4)]
    outs = [q.communicate() for q in procs]
    assert all(q.returncode == 0 for q in procs), outs
    g = og.Store(tmp_path, "cc").load()
    assert sorted(n["id"] for n in g["nodes"]) == ["t1", "t2", "t3", "t4", "t5"] and g["plan_version"] == 5
    assert sorted(n["title"] for n in g["nodes"] if n["id"] != "t1") == [f"song song {k}" for k in range(4)]


def test_review_V6_add_node_rejects_structure_injection(tmp_path):
    r, p = _build(tmp_path, "# I\n" + _task(1) + _task(2), "inj"); before = p.read_text()
    for field, val in (("--produces", "x\n### Task 7: injected"), ("--verify", "true\n**Depends:** Task 2"), ("--files", "a.py\n- Sửa: `b.py`"), ("--title", "a\nb")):
        args = ["add-node", "inj", "--title", "T", "--files", "z.py", "--verify", "true", "--no-viz"]
        args = [x for x in args] + [field, val] if field != "--title" else ["add-node", "inj", "--title", val, "--no-viz"]
        r = run(tmp_path, *args)
        assert r.returncode != 0 and "xuống dòng" in r.stderr, (field, r.stdout, r.stderr)
    assert p.read_text() == before
    assert run(tmp_path, "add-node", "inj", "--title", "T", "--depends", "Task 1:DATA,t2:scheduling_preference", "--files", "z.py", "--verify", "true", "--no-viz").returncode == 0
    assert {x["id"]: x for x in og.Store(tmp_path, "inj").load()["nodes"]}["t3"]["dep_reasons"] == {"t1": "data", "t2": "preference"}


def test_review_add_node_refuses_blocks_on_running_node_and_mismatched_plan(tmp_path):
    r, p = _build(tmp_path, "# B\n" + _task(1) + _task(2), "bk")
    assert run(tmp_path, "lock", "bk", "t1").returncode == 0
    r = run(tmp_path, "add-node", "bk", "--title", "T", "--blocks", "t1", "--files", "z.py", "--verify", "true", "--no-viz")
    assert r.returncode != 0 and "đang locked" in r.stderr
    p.write_text(p.read_text() + _task(5), encoding="utf-8")                              # PLAN sửa tay mà chưa build
    r = run(tmp_path, "add-node", "bk", "--title", "T", "--files", "z.py", "--verify", "true", "--no-viz")
    assert r.returncode != 0 and "không khớp graph" in r.stderr


def test_VT27_review_C4_run_result_from_old_plan_is_stale_after_midrun_replan(tmp_path):
    """VT-27 đường `run`: replan GIỮA lúc agent chạy → kết quả thuộc plan cũ, không publish; node về ready để làm lại theo spec mới."""
    r, p = _build(tmp_path, "# V\n" + _task(1), "vv"); assert r.returncode == 0
    env = dict(os.environ, ORCA_GRAPH_HOME=str(tmp_path / "home"), ORCA_GRAPH_NO_DAEMON="1")
    q = subprocess.Popen([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "run", "vv", "t1", "--hb", "0.2", "--", "sh", "-c", "sleep 1.5"],
                         cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(0.7)
    p.write_text(p.read_text().replace("việc 1", "việc 1 ĐỔI").replace("f1.py", "g1.py"), encoding="utf-8")
    assert run(tmp_path, "build", str(p)).returncode == 0
    out, err = q.communicate()
    n = og.Store(tmp_path, "vv").load()["nodes"][0]
    assert "STALE" in out and n["state"] == "ready" and n["verified"] == "unverified", out + err
    assert q.returncode != 0 and not (tmp_path / "vv.locks" / "t1").exists()


def test_review_V1_V2_expired_or_unlocked_holder_does_not_block_other_graph_forever(tmp_path):
    a, _ = setup_res(tmp_path, ["head"], "ha"); b, _ = setup_res(tmp_path, ["head"], "hb")
    assert run(tmp_path, "lock", a, "t1", "--lease-sec", "1").returncode == 0
    time.sleep(1.3)
    r = run(tmp_path, "lock", b, "t1")                                                    # lease của ha/t1 đã hết — không ai gọi `next ha`
    assert r.returncode == 0, r.stderr
    assert {n["id"]: n["state"] for n in og.Store(tmp_path, a).load()["nodes"]}["t1"] == "unknown"
    assert run(tmp_path, "unlock", b, "t1").returncode == 0                               # unlock tay: state phải rời `locked`, không thì giữ claim mãi
    assert {n["id"]: n["state"] for n in og.Store(tmp_path, b).load()["nodes"]}["t1"] == "unknown"
    c, _ = setup_res(tmp_path, ["head"], "hc")
    assert run(tmp_path, "lock", c, "t1").returncode == 0


def test_VT03_review_V4_capacity_is_a_pool_size_not_order_dependent(tmp_path):
    """Pool của key = capacity NHỎ NHẤT đã khai; holder shared cũng chiếm slot; capacity:0 bị từ chối."""
    gid, _ = setup_res(tmp_path, ["api(capacity:1)", "api(capacity:3)", "api(shared)"], "cp")
    assert run(tmp_path, "lock", gid, "t1").returncode == 0
    for n in ("t2", "t3"):                                                                # t1 khai pool = 1 → không ai vào thêm, kể cả `shared`
        r = run(tmp_path, "lock", gid, n); assert r.returncode != 0 and "capacity 1/1" in r.stderr, (n, r.stderr)
    gid2, _ = setup_res(tmp_path, ["q(shared)", "q(shared)", "q(capacity:2)"], "cq")
    assert run(tmp_path, "lock", gid2, "t1").returncode == 0 and run(tmp_path, "lock", gid2, "t2").returncode == 0
    r = run(tmp_path, "lock", gid2, "t3"); assert r.returncode != 0 and "capacity 2/2" in r.stderr
    p = tmp_path / "z-PLAN.md"; p.write_text(res_plan(["k(capacity:0)"]), encoding="utf-8")
    assert run(tmp_path, "build", str(p)).returncode != 0


def test_VT01_review_V5_audit_keeps_preference_edge_when_downstream_uses_upstream_output(tmp_path):
    plan = "# A\n### Task 1: parser\n**Files:**\n- Tạo: `parser.py`\n**Interfaces:**\n- Consumes: —\n- Produces: parse(text) -> list\n**Verify:** `true`\n" \
           "### Task 2: dùng parser\n**Files:**\n- Tạo: `use.py`\n**Interfaces:**\n- Consumes: parse(text) -> list từ parser.py\n- Produces: `x`\n**Depends:** Task 1 (preference)\n**Verify:** `python3 -c \"import parser\"`\n"
    r, _ = _build(tmp_path, plan, "au"); assert r.returncode == 0, r.stderr
    out = json.loads(run(tmp_path, "audit-edges", "au", "--json").stdout)
    assert out["diff"]["remove_preference_edges"] == [] and out["findings"][0]["code"] == "PREFERENCE_HAS_HIDDEN_CONSTRAINT"


def test_review_V3_admission_mutex_has_no_overlap_under_contention(tmp_path):
    """6 process giành mutex 40 vòng: không bao giờ có 2 process cùng ở trong vùng găng (bản O_EXCL+mtime từng lọt 1/150)."""
    code = ("import importlib.util,sys,time,os;from pathlib import Path\n"
            f"s=importlib.util.spec_from_file_location('og',r'{SCRIPT}');og=importlib.util.module_from_spec(s);s.loader.exec_module(og)\n"
            "d=Path(sys.argv[1])\nfor _ in range(40):\n with og.admission_mutex(d):\n  f=d/'in';assert not f.exists(),'OVERLAP';f.write_text('x');time.sleep(0.002);f.unlink()\n")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(tmp_path)], stderr=subprocess.PIPE, text=True) for _ in range(6)]
    errs = [q.communicate()[1] for q in procs]
    assert all(q.returncode == 0 for q in procs), [e[-300:] for e in errs if e]


def test_VT10_review_T2_reconcile_items_never_drops_error_rows_or_defaults_to_complete(tmp_path):
    v = _verdicts(tmp_path, [{"item_id": "A", "verdict": "supported"}, {"item_id": "A", "status": "timeout", "verdict": None}])
    assert json.loads(run(tmp_path, "reconcile-items", "--expected", "A", "--verdicts", v).stdout) == {"protocol_error": "CONFLICTING_OUTCOME"}
    v = _verdicts(tmp_path, [{"item_id": "A", "verdict": "supported"}, {"item_id": "Z", "verdict": None}])
    assert json.loads(run(tmp_path, "reconcile-items", "--expected", "A", "--verdicts", v).stdout) == {"protocol_error": "UNEXPECTED_ITEM_ID"}
    r = run(tmp_path, "reconcile-items", "--expected", "A", "--verdicts", str(tmp_path / "khong-co.jsonl"))
    assert r.returncode == 2 and "VERDICTS_FILE_NOT_FOUND" in r.stdout
    r = run(tmp_path, "reconcile-items", "--verdicts", v)
    assert r.returncode == 2 and "EMPTY_MANIFEST" in r.stdout


def test_store_ignores_its_own_runtime_lock_files(tmp_path):
    gid = setup(tmp_path); assert run(tmp_path, "lock", gid, "t1").returncode == 0
    gi = (tmp_path / ".gitignore").read_text()
    assert ".admission.lock*" in gi and "*.locks/" in gi
    (tmp_path / ".gitignore").write_text("của user\n"); run(tmp_path, "lock", gid, "t3")
    assert (tmp_path / ".gitignore").read_text() == "của user\n"          # đã có thì KHÔNG đụng


def test_manual_lock_registers_store_and_starts_the_watch_daemon(tmp_path):
    """Hồi quy 200926 "control room chết trong âm thầm": dispatch GÕ TAY cũng phải có daemon canh, không chỉ `run`."""
    gid = setup(tmp_path); home = tmp_path / "home"
    env = {k: v for k, v in os.environ.items() if k != "ORCA_GRAPH_NO_DAEMON"}; env["ORCA_GRAPH_HOME"] = str(home)
    r = subprocess.run([sys.executable, str(SCRIPT), "--dir", str(tmp_path), "lock", gid, "t1"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 0, r.stderr
    try:
        assert str(tmp_path.resolve()) in json.loads((home / "registry.json").read_text())["dirs"]
        for _ in range(50):
            if (home / "daemon.lock").exists():
                break
            time.sleep(0.1)
        pid = int((home / "daemon.lock").read_text()); os.kill(pid, 0)                # daemon SỐNG
    finally:
        try:
            os.kill(int((home / "daemon.lock").read_text()), signal.SIGTERM)
        except Exception:
            pass


def test_generated_html_embeds_lexend_deca_light_offline(tmp_path):
    """Mọi trang engine sinh ra dùng font mặc định Lexend Deca Light, NHÚNG base64 — mở file:// không mạng vẫn đúng font."""
    gid = setup(tmp_path)
    r = subprocess.run([sys.executable, str(ROOT / "engine/graph-viz.py"), str(tmp_path / f"{gid}.graph.json")], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, str(ROOT / "engine/graph-atlas.py"), str(tmp_path)], capture_output=True, text=True, cwd=ROOT)
    assert r.returncode == 0, r.stderr
    for page in (tmp_path / f"{gid}.graph.html", tmp_path / "atlas.html"):
        h = page.read_text(encoding="utf-8")
        assert h.count('id="ovs-font"') == 1 and "font-family:'Lexend Deca'" in h and "data:font/woff2;base64," in h, page
        assert "--fw-text:300" in h and "fonts.googleapis.com" not in h
    assert subprocess.run([sys.executable, str(ROOT / "engine/html_font.py"), "--check"], capture_output=True).returncode == 0


def test_state_badge_text_meets_contrast_on_every_state_colour():
    """Huy hiệu trạng thái từng để chữ TRẮNG trên cam/vàng/lục (2–3:1). ink_on() phải cho ≥ 4.5:1 với MỌI màu trạng thái."""
    import importlib.util
    s = importlib.util.spec_from_file_location("gv", ROOT / "engine/graph-viz.py"); gv = importlib.util.module_from_spec(s); s.loader.exec_module(gv)
    lin = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    L = lambda h: sum(w * lin(int(h.lstrip("#")[i:i + 2], 16) / 255) for w, i in ((0.2126, 0), (0.7152, 2), (0.0722, 4)))
    for state, bg in gv.STATE_COLOR.items():
        fg = gv.ink_on(bg); a, b = sorted((L(fg), L(bg)), reverse=True)
        assert (a + 0.05) / (b + 0.05) >= 4.5, (state, bg, fg)
        assert f"color:{fg}" in gv.state_badge_style(state)
    css = gv.CSS
    assert "border-left-color:var(--accent)" not in css and ".chip b{color:var(--accent-ink)}" in css
