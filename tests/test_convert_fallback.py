import sys
import manifest as M
import convert as C


def test_second_upload_is_skipped_after_a_timeout(args, monkeypatch, tmp_path):
    m = M.load(args)
    d = M.topic_dir(args)
    (d / "pdf").mkdir()
    for pid in ("p1", "p2"):
        m["papers"][pid] = M.new_paper(pid, title=pid, status="pdf")
        (d / "pdf" / f"{pid}.pdf").write_bytes(b"%PDF-1.4 fake")
    M.save(args, m)
    calls = []

    def fake_convert_one(pid, mode, source, out_dir, a, abort):
        calls.append((pid, mode))
        return {"id": pid, "status": "failed", "error": "upload: Put https://oss: Client.Timeout exceeded", "stderr": ""}
    monkeypatch.setattr(C, "convert_one", fake_convert_one)
    monkeypatch.setattr(C, "local_text_md", lambda pdf, md, t: (md.parent.mkdir(parents=True, exist_ok=True), md.write_text("x" * 5000), "")[2])
    monkeypatch.setattr(C, "token_gate", lambda: "")
    monkeypatch.setattr(C, "sources_of", lambda p, pdf, up: [("upload", str(pdf))])
    sys.argv = ["convert.py", "--topic", args.topic, "--root", args.root, "--jobs", "1"]
    assert C.main() == 0
    assert [c[0] for c in calls] == ["p1"]            # p2 never tried the upload
    m = M.load(args)
    assert all(m["papers"][p]["status"] == "md" and m["papers"][p].get("conversion") == "local-text" for p in ("p1", "p2"))


def test_second_upload_is_skipped_after_our_own_lowercase_timeout(args, monkeypatch, tmp_path):
    # convert_one's own subprocess.TimeoutExpired path (:240) writes a lowercase
    # "timeout after Ns" error, unlike the Go-style "Client.Timeout" the CLI itself can print.
    m = M.load(args)
    d = M.topic_dir(args)
    (d / "pdf").mkdir()
    for pid in ("p1", "p2"):
        m["papers"][pid] = M.new_paper(pid, title=pid, status="pdf")
        (d / "pdf" / f"{pid}.pdf").write_bytes(b"%PDF-1.4 fake")
    M.save(args, m)
    calls = []

    def fake_convert_one(pid, mode, source, out_dir, a, abort):
        calls.append((pid, mode))
        return {"id": pid, "status": "failed", "error": f"timeout after {int(a.timeout)}s", "stderr": ""}
    monkeypatch.setattr(C, "convert_one", fake_convert_one)
    monkeypatch.setattr(C, "local_text_md", lambda pdf, md, t: (md.parent.mkdir(parents=True, exist_ok=True), md.write_text("x" * 5000), "")[2])
    monkeypatch.setattr(C, "token_gate", lambda: "")
    monkeypatch.setattr(C, "sources_of", lambda p, pdf, up: [("upload", str(pdf))])
    sys.argv = ["convert.py", "--topic", args.topic, "--root", args.root, "--jobs", "1"]
    assert C.main() == 0
    assert [c[0] for c in calls] == ["p1"]            # p2 never tried the upload
    m = M.load(args)
    assert all(m["papers"][p]["status"] == "md" and m["papers"][p].get("conversion") == "local-text" for p in ("p1", "p2"))
