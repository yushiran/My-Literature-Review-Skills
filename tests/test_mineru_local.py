import json
import subprocess
import zipfile

import mineru_local as ML
from types import SimpleNamespace


def fake_setup(root, device="cpu", tier="basic", spec=ML.SPEC_CPU, version="4.0.1"):
    """A root that looks like a finished --setup, without a byte of mineru in it."""
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "mineru-kit").write_text("#!/bin/sh\n")
    (root / "home" / "models" / "MinerU-4_models_onnx").mkdir(parents=True)
    (root / "setup.json").write_text(json.dumps(
        {"mineru": version, "spec": spec, "device": device, "tier": tier,
         "source": "huggingface", "date": "2026-09-17"}))
    return root


# ---------------------------------------------------------------- device


def test_device_is_cuda_when_nvidia_smi_lists_a_gpu(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="GPU 0: NVIDIA GH200 120GB (UUID: GPU-x)\n", stderr=""))
    assert ML.pick_device("auto")[0] == "cuda"


def test_device_is_cpu_when_there_is_no_nvidia_smi(monkeypatch):
    def missing(*a, **k):
        raise FileNotFoundError("nvidia-smi")
    monkeypatch.setattr(subprocess, "run", missing)
    device, why = ML.pick_device("auto")
    assert device == "cpu" and "no GPU" in why


def test_device_is_cpu_when_nvidia_smi_lists_none(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="\n", stderr=""))
    assert ML.pick_device("auto")[0] == "cpu"


def test_an_explicit_device_never_asks_nvidia_smi(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("nvidia-smi must not run when --device is explicit")
    monkeypatch.setattr(subprocess, "run", boom)
    assert ML.pick_device("cpu")[0] == "cpu"
    assert ML.pick_device("cuda")[0] == "cuda"


def test_the_cpu_device_hides_the_gpu_from_torch(tmp_path):
    env = ML.run_env(tmp_path, "cpu", "huggingface")
    assert env["CUDA_VISIBLE_DEVICES"] == ""
    assert env["MINERU_MODEL_SMALL_BACKEND"] == "onnx"
    # The hub cache is redirected too: a model download must not fill the home quota.
    assert env["HF_HOME"] == str(tmp_path / "hf")
    assert env["MINERU_HOME"] == str(tmp_path / "home")


def test_the_cuda_device_asks_for_the_torch_backend(tmp_path):
    env = ML.run_env(tmp_path, "cuda", "huggingface")
    assert env["MINERU_MODEL_SMALL_BACKEND"] == "torch"
    assert "CUDA_VISIBLE_DEVICES" not in env or env["CUDA_VISIBLE_DEVICES"] != ""


# ---------------------------------------------------------------- output mapping


def write_zip(path, entries):
    with zipfile.ZipFile(str(path), "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def test_the_mineru_tree_becomes_the_skill_layout(tmp_path):
    body = "# Title\n\n![](images/page_1_image_body_1.jpg)\n" + "x" * 500
    z = write_zip(tmp_path / "ib.zip", {
        "markdown.md": body,
        "images/page_1_image_body_1.jpg": b"\xff\xd8jpeg",
        "images/page_2_equation_6.jpg": b"\xff\xd8jpeg",
        "middle_json.json": "{}",
        "model_output.json": "{}",
        "structured_content.json": "{}",
    })
    out = tmp_path / "md" / "2025-zheng-inversebench"
    md = ML.unpack(z, out, "2025-zheng-inversebench")
    assert md == out / "2025-zheng-inversebench.md"
    assert md.read_text() == body
    assert (out / "images" / "page_1_image_body_1.jpg").read_bytes() == b"\xff\xd8jpeg"
    assert sorted(p.name for p in (out / "images").iterdir()) == [
        "page_1_image_body_1.jpg", "page_2_equation_6.jpg"]
    # The link in the markdown resolves where it now sits, and the sidecars are gone.
    assert not (out / "middle_json.json").exists()
    assert not (out / "model_output.json").exists()
    assert {p.name for p in out.iterdir()} == {"2025-zheng-inversebench.md", "images"}


def test_a_re_run_drops_the_previous_figures(tmp_path):
    out = tmp_path / "md" / "p1"
    (out / "images").mkdir(parents=True)
    (out / "images" / "stale.jpg").write_bytes(b"old")
    z = write_zip(tmp_path / "p1.zip", {"markdown.md": "x" * 500,
                                        "images/fresh.jpg": b"new"})
    ML.unpack(z, out, "p1")
    assert [p.name for p in (out / "images").iterdir()] == ["fresh.jpg"]


def test_a_zip_entry_cannot_escape_the_output_directory(tmp_path):
    out = tmp_path / "md" / "p1"
    z = write_zip(tmp_path / "p1.zip", {"markdown.md": "x" * 500,
                                        "images/../../escaped.jpg": b"no"})
    ML.unpack(z, out, "p1")
    assert not (tmp_path / "escaped.jpg").exists()
    assert not (tmp_path / "md" / "escaped.jpg").exists()


def test_a_zip_with_no_markdown_is_no_conversion(tmp_path):
    z = write_zip(tmp_path / "p1.zip", {"middle_json.json": "{}"})
    assert ML.unpack(z, tmp_path / "md" / "p1", "p1") is None


# ---------------------------------------------------------------- setup


def test_setup_on_a_finished_install_runs_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("LITREV_MINERU_HOME", str(tmp_path / "root"))
    fake_setup(tmp_path / "root")
    monkeypatch.setattr(ML, "nvidia_smi_gpu", lambda: False)   # so the marker's cpu matches

    def boom(*a, **k):
        raise AssertionError(f"--setup must install nothing when it is already set up: {a}")
    monkeypatch.setattr(ML, "run", boom)
    monkeypatch.setattr(subprocess, "run", boom)
    args = SimpleNamespace(device="auto", tier="basic", source="huggingface", force=False)
    assert ML.setup(args) == 0


def gpu_setup_run(tmp_path, monkeypatch, cuda_major):
    """Run setup() on a fake GPU machine; returns the commands it would have run."""
    root = tmp_path / "root"
    monkeypatch.setenv("LITREV_MINERU_HOME", str(root))
    monkeypatch.setattr(ML, "nvidia_smi_gpu", lambda: True)
    monkeypatch.setattr(ML, "driver_cuda_major", lambda: cuda_major)
    monkeypatch.setattr(ML, "uv_ok", lambda: True)
    monkeypatch.setattr(ML, "free_gb", lambda p: 999.0)
    monkeypatch.setattr(ML, "torch_state", lambda r: ("2.11.0+cu128", True))
    monkeypatch.setattr(ML, "mineru_version", lambda r: "4.0.1")
    cmds = []

    def fake_run(cmd, timeout=None, env=None):
        cmds.append(" ".join(str(c) for c in cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(ML, "run", fake_run)
    args = SimpleNamespace(device="auto", tier="basic", source="huggingface", force=False)
    assert ML.setup(args) == 0
    assert json.loads((root / "setup.json").read_text())["device"] == "cuda"
    return cmds


def test_the_matching_torch_goes_in_before_mineru(tmp_path, monkeypatch):
    # A CUDA 12 driver cannot run PyPI's CUDA 13 torch. Installing the right one first is
    # what keeps a second set of CUDA libraries, 4 GB of them, out of the venv.
    cmds = gpu_setup_run(tmp_path, monkeypatch, cuda_major=12)
    torch_at = next(i for i, c in enumerate(cmds) if ML.TORCH_INDEX in c)
    mineru_at = next(i for i, c in enumerate(cmds) if ML.SPEC_GPU in c)
    assert torch_at < mineru_at


def test_a_cuda_13_driver_takes_the_torch_pypi_serves(tmp_path, monkeypatch):
    cmds = gpu_setup_run(tmp_path, monkeypatch, cuda_major=13)
    assert not any(ML.TORCH_INDEX in c for c in cmds)
    assert any(ML.SPEC_GPU in c for c in cmds)


def test_setup_runs_again_when_the_marker_names_another_device(tmp_path, monkeypatch):
    monkeypatch.setattr(ML, "nvidia_smi_gpu", lambda: False)
    monkeypatch.setenv("LITREV_MINERU_HOME", str(tmp_path / "root"))
    fake_setup(tmp_path / "root", device="cuda", spec=ML.SPEC_GPU)
    monkeypatch.setattr(ML, "uv_ok", lambda: False)   # stops right after the marker check
    args = SimpleNamespace(device="auto", tier="basic", source="huggingface", force=False)
    assert ML.setup(args) == 2


def test_is_setup_wants_a_marker_a_cli_and_the_models(tmp_path, monkeypatch):
    root = tmp_path / "root"
    monkeypatch.setenv("LITREV_MINERU_HOME", str(root))
    assert ML.is_setup() is False
    fake_setup(root)
    assert ML.is_setup() is True
    (root / "venv" / "bin" / "mineru-kit").unlink()
    assert ML.is_setup() is False


def test_the_root_follows_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("LITREV_MINERU_HOME", str(tmp_path / "elsewhere"))
    assert ML.root_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("LITREV_MINERU_HOME")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    assert ML.root_dir() == tmp_path / "xdg" / "litrev" / "mineru"


def test_check_warns_when_the_node_caps_memory_below_one_conversion(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LITREV_MINERU_HOME", str(tmp_path / "root"))
    fake_setup(tmp_path / "root")
    monkeypatch.setattr(ML, "mem_limit_gb", lambda: 4.0)       # what a login node gives a user
    monkeypatch.setattr(ML, "nvidia_smi_gpu", lambda: False)
    monkeypatch.setattr(ML, "mineru_version", lambda r: "4.0.1")
    monkeypatch.setattr(ML, "torch_state", lambda r: ("", False))
    assert ML.check(SimpleNamespace(device="auto")) == 0
    out = capsys.readouterr().out
    assert "warning:" in out and "4.0 GB cap" in out


def test_convert_refuses_before_setup(tmp_path, monkeypatch):
    monkeypatch.setenv("LITREV_MINERU_HOME", str(tmp_path / "root"))
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    res = ML.convert(tmp_path / "a.pdf", tmp_path / "out", "p1")
    assert res["status"] == "failed" and "not set up" in res["error"]
