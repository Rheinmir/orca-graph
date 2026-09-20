#!/usr/bin/env python3
"""Eval orca-graph theo ma trận nghiệm thu VT-01…28 của Reprise PRD v1.1 §30.1.

  run.py            chạy pytest, ghi scoreboard.json + nối một dòng vào history.jsonl (theo dõi xu hướng qua các bản)
  run.py --check    chỉ chấm, KHÔNG ghi file; rc 1 khi ma trận sai dạng hoặc có VT covered/partial mà đỏ/không có test

Luật ghép: VT `VT-09` khớp mọi testcase có `VT09` trong tên. Row covered/partial pass ⇔ có ≥1 test khớp và tất cả đều xanh.
"""
import hashlib, json, subprocess, sys, tempfile, time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent


def find(*cands):
    for c in cands:
        if c.exists():
            return c
    raise SystemExit(f"không thấy: {[str(c) for c in cands]}")


TESTS = find(HERE.parent / "tests" / "test_orca_graph.py")
ENGINE = find(HERE.parent / "engine" / "orca-graph.py")


def main():
    check = "--check" in sys.argv
    m = json.loads((HERE / "vt-matrix.json").read_text(encoding="utf-8"))
    rows = m["rows"]
    ids = [r["id"] for r in rows]
    problems = []
    if ids != [f"VT-{i:02d}" for i in range(1, 29)]:
        problems.append("ma trận phải có đúng VT-01…VT-28 theo thứ tự")
    problems += [f"{r['id']}: {r['status']} thiếu reason" for r in rows if r["status"] in ("out_of_scope", "partial") and not r.get("reason")]
    problems += [f"{r['id']}: status lạ {r['status']}" for r in rows if r["status"] not in ("covered", "partial", "out_of_scope")]
    with tempfile.TemporaryDirectory() as td:
        xml = Path(td) / "j.xml"
        subprocess.run([sys.executable, "-m", "pytest", "-q", str(TESTS), "-k", "VT", f"--junitxml={xml}", "-p", "no:cacheprovider"], capture_output=True, text=True)
        cases = [(c.get("name"), not any(ch.tag in ("failure", "error", "skipped") for ch in c)) for c in ET.parse(xml).getroot().iter("testcase")] if xml.exists() else []
    out_rows = []
    for r in rows:
        code = r["id"].replace("-", "")
        hits = [(n, ok) for n, ok in cases if code in n]
        if r["status"] == "out_of_scope":
            res = "n/a"
            if hits:
                problems.append(f"{r['id']}: ghi out_of_scope nhưng có test {hits[0][0]} — cập nhật ma trận")
        else:
            res = "pass" if hits and all(ok for _, ok in hits) else "FAIL" if hits else "NO_TEST"
        out_rows.append({**r, "tests": [n for n, _ in hits], "result": res})
    scored = [r for r in out_rows if r["status"] != "out_of_scope"]
    board = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "engine_sha256": hashlib.sha256(ENGINE.read_bytes()).hexdigest()[:16],
             "total": len(rows), "covered": sum(r["status"] == "covered" for r in rows), "partial": sum(r["status"] == "partial" for r in rows),
             "out_of_scope": sum(r["status"] == "out_of_scope" for r in rows), "passed": sum(r["result"] == "pass" for r in scored),
             "failed": [r["id"] for r in scored if r["result"] != "pass"], "matrix_problems": problems, "rows": out_rows}
    for r in out_rows:
        print(f"  {r['id']}  {r['status']:<13} {r['result']:<8} {r['behavior'][:70]}")
    print(f"VT: {board['passed']}/{len(scored)} trong phạm vi pass · {board['out_of_scope']}/28 ngoài phạm vi (có lý do) · engine {board['engine_sha256']}")
    for p in problems:
        print(f"  ✗ {p}")
    if not check:
        (HERE / "scoreboard.json").write_text(json.dumps(board, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        with open(HERE / "history.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({k: board[k] for k in ("ts", "engine_sha256", "covered", "partial", "out_of_scope", "passed", "failed")}, ensure_ascii=False) + "\n")
        print(f"→ {HERE / 'scoreboard.json'}")
    sys.exit(1 if board["failed"] or problems else 0)


if __name__ == "__main__":
    main()
