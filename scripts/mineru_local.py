# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Local MinerU: install the open-source package, then convert a pdf without the hosted API.

Everything lives under one root, `LITREV_MINERU_HOME`, else `$XDG_CACHE_HOME/litrev/mineru`,
else `~/.cache/litrev/mineru` — never inside the repo and never in the user's own `~/.mineru`:

    <root>/venv        a uv venv holding `mineru`; 6.0 GB with the torch extra, 0.7 GB without
    <root>/home        MINERU_HOME: config.yaml and models/ (0.8 GB basic, +1.2 GB standard)
    <root>/hf          HF_HOME, so a model download cannot fill the user's home quota
    <root>/setup.json  what --setup installed; the marker that makes a second --setup a no-op

`--setup` installs and downloads, `--check` reports, `--convert` runs one pdf and writes the
skill's own layout: `<out>/<id>.md` with its `images/` beside it. Device: cuda when nvidia-smi
lists a GPU, else cpu, and `--device` overrides. MinerU chooses the small-model backend from
the device, torch on a GPU and onnx on a CPU, so `--device cpu` hides the GPU from torch and
asks for onnx. The tier is `basic` by default: small models only, all of them on the GPU.
`standard` adds a 1.2 GB VLM run through llama.cpp, which is slower and, on aarch64, exits
with a segmentation fault after writing its output.

Exit codes follow the rest of the skill: 0 ok · 1 usage · 2 the local backend is unavailable
(not installed, install failed, no disk) · 4 nothing to do. Contract: references/workflow.md.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manifest as M  # noqa: E402

SPEC_GPU = "mineru[torch]>=4.0,<5"      # torch small models, i.e. the GPU path
SPEC_CPU = "mineru>=4.0,<5"             # base package: onnx small models on the CPU
PYTHON = "3.12"                         # MinerU supports >=3.10,<3.15; its docs recommend 3.12
# Used only when the torch wheel PyPI serves cannot talk to this machine's driver.
TORCH_INDEX = os.environ.get("LITREV_TORCH_INDEX", "https://download.pytorch.org/whl/cu128")
TIERS = ("flash", "basic", "standard")
SOURCES = ("auto", "huggingface", "modelscope")
MIN_MD_BYTES = 200
SETUP_GB = 8.0          # venv + basic models + room to unpack
RUN_GB = 6.0            # one 29-page conversion peaked at 4.5 GB resident
SMI_TIMEOUT = 20
PROBE_TIMEOUT = 300     # importing torch in a cold venv on a shared filesystem is slow
INSTALL_TIMEOUT = 3600
DOWNLOAD_TIMEOUT = 3600
CONVERT_TIMEOUT = 1800
MAX_ERROR = 300
QUOTA = "Disk quota exceeded"


# ---------------------------------------------------------------- layout

def root_dir() -> Path:
    env = os.environ.get("LITREV_MINERU_HOME")
    if env:
        return Path(env).expanduser()
    cache = os.environ.get("XDG_CACHE_HOME") or "~/.cache"
    return Path(cache).expanduser() / "litrev" / "mineru"


def venv_python(root: Path) -> Path:
    return root / "venv" / "bin" / "python"


def cli(root: Path) -> Path:
    return root / "venv" / "bin" / "mineru-kit"


def home_dir(root: Path) -> Path:
    return root / "home"


def models_dir(root: Path) -> Path:
    return home_dir(root) / "models"


def marker_path(root: Path) -> Path:
    return root / "setup.json"


def read_marker(root: Path) -> dict:
    try:
        data = json.loads(marker_path(root).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def is_setup(root=None) -> bool:
    """True when --setup has finished here. What convert.py asks before taking this route."""
    root = root or root_dir()
    marker = read_marker(root)
    if not marker.get("mineru"):
        return False
    return cli(root).exists() and models_dir(root).is_dir() and any(models_dir(root).iterdir())


SETUP_MSG = ("convert: local MinerU is not set up; it needs about 7 GB of disk and one download. "
             "Run: uv run scripts/mineru_local.py --setup")


# ---------------------------------------------------------------- device and limits

def nvidia_smi_gpu() -> bool:
    """True when nvidia-smi lists at least one GPU."""
    try:
        r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True,
                           timeout=SMI_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and "GPU" in (r.stdout or "")


def driver_cuda_major() -> int:
    """Major CUDA version this driver speaks, from nvidia-smi's header; 0 when unknown."""
    try:
        r = subprocess.run(["nvidia-smi"], capture_output=True, text=True,
                           timeout=SMI_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return 0
    m = re.search(r"CUDA Version:\s*(\d+)\.", r.stdout or "")
    return int(m.group(1)) if m else 0


def pick_device(pref: str) -> tuple:
    """(device, why). auto = cuda when nvidia-smi lists a GPU, else cpu."""
    if pref in ("cuda", "cpu"):
        return pref, f"asked for with --device {pref}"
    if nvidia_smi_gpu():
        return "cuda", "nvidia-smi lists a GPU"
    return "cpu", "no GPU: nvidia-smi is absent or lists none"


def torch_state(root: Path) -> tuple:
    """(version, cuda_available) of the torch inside the venv; ('', False) when it has none."""
    py = venv_python(root)
    if not py.exists():
        return "", False
    prog = "import torch; print(torch.__version__, torch.cuda.is_available())"
    try:
        r = subprocess.run([str(py), "-c", prog], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return "", False
    parts = (r.stdout or "").split()
    if r.returncode != 0 or len(parts) < 2:
        return "", False
    return parts[0], parts[1] == "True"


def mem_limit_gb() -> float:
    """Smallest cgroup memory cap above this process, in GB; 0 when there is none to read.

    The login node caps a user at 4 GB, which is less than one conversion needs.
    """
    limits = []
    try:
        line = Path("/proc/self/cgroup").read_text().strip().splitlines()[0]
        rel = line.rpartition(":")[2].lstrip("/")
    except (OSError, IndexError):
        return 0.0
    node = Path("/sys/fs/cgroup") / rel
    while True:
        try:
            raw = (node / "memory.max").read_text().strip()
            if raw.isdigit():
                limits.append(int(raw) / 2 ** 30)
        except OSError:
            pass
        if node == Path("/sys/fs/cgroup") or node.parent == node:
            break
        node = node.parent
    return min(limits) if limits else 0.0


def free_gb(path: Path) -> float:
    probe = path
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(str(probe)).free / 2 ** 30
    except OSError:
        return 0.0


# ---------------------------------------------------------------- running mineru

def run_env(root: Path, device: str, source: str) -> dict:
    """The environment every mineru subprocess gets: its own MINERU_HOME, HF_HOME and backend."""
    env = dict(os.environ)
    env["MINERU_HOME"] = str(home_dir(root))
    env["HF_HOME"] = str(root / "hf")           # keeps the hub cache off the user's home quota
    env["MINERU_MODEL_SOURCE"] = source
    env["MINERU_MODEL_SMALL_BACKEND"] = "torch" if device == "cuda" else "onnx"
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""        # a GPU box asked for cpu: hide it from torch too
    return env


def last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:MAX_ERROR] if lines else ""


def run(cmd: list, timeout: float, env=None) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          timeout=timeout, env=env, stdin=subprocess.DEVNULL)


# ---------------------------------------------------------------- setup

def uv_ok() -> bool:
    return shutil.which("uv") is not None


def install(root: Path, spec: str) -> str:
    """uv pip install into the venv. Returns '' on success, else the message to print.

    Never `-U`: a second run would then upgrade the CUDA libraries out from under a torch
    that fix_torch_for_gpu pinned, and the venv dies with `undefined symbol: ncclCommResume`.
    """
    env = dict(os.environ)
    env.setdefault("UV_LINK_MODE", "copy")      # the venv and the uv cache are often different mounts
    try:
        r = run(["uv", "pip", "install", "--python", venv_python(root), spec],
                timeout=INSTALL_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return f"install timed out after {INSTALL_TIMEOUT}s"
    if r.returncode == 0:
        return ""
    out = (r.stdout or "") + (r.stderr or "")
    if QUOTA in out:
        return (f"install ran out of disk at {root}: {QUOTA}. Point LITREV_MINERU_HOME at a "
                f"filesystem with {SETUP_GB:.0f} GB free and run --setup again.")
    return "install failed: " + last_line(out)


def install_torch_for_gpu(root: Path) -> str:
    """Install torch from a CUDA index that matches this machine's driver.

    PyPI ships a torch built against the newest CUDA; a machine one driver generation
    behind gets 'the NVIDIA driver on your system is too old' and silently runs on the CPU.
    Run before `mineru[torch]`, it leaves that requirement already met, so the wrong CUDA
    libraries are never downloaded. Run after, it replaces torch but leaves them in the
    venv, several GB of them, because nothing uninstalls an orphan.
    """
    env = dict(os.environ)
    env.setdefault("UV_LINK_MODE", "copy")
    cmd = ["uv", "pip", "install", "--python", venv_python(root), "--index-url", TORCH_INDEX,
           "--reinstall-package", "torch", "--reinstall-package", "torchvision",
           "torch>=2.7,<3", "torchvision"]
    try:
        r = run(cmd, timeout=INSTALL_TIMEOUT, env=env)
    except subprocess.TimeoutExpired:
        return f"torch reinstall timed out after {INSTALL_TIMEOUT}s"
    if r.returncode != 0:
        return "torch reinstall failed: " + last_line((r.stdout or "") + (r.stderr or ""))
    return ""


def write_config(root: Path, source: str) -> None:
    """MINERU_HOME/config.yaml, so the CLI is usable by hand with the same models."""
    home_dir(root).mkdir(parents=True, exist_ok=True)
    (home_dir(root) / "config.yaml").write_text(
        "# written by the literature-review skill (scripts/mineru_local.py --setup)\n"
        "model:\n"
        f"  source: {source}\n"
        f"  base_dir: {models_dir(root)}\n"
    )


def download_models(root: Path, device: str, tier: str, source: str) -> str:
    """mineru-kit models download for this tier and backend. '' on success, else the message."""
    backend = "torch" if device == "cuda" else "onnx"
    cmd = [cli(root), "models", "download", "--tier", tier,
           "--small-backend", backend, "--source", source]
    try:
        r = run(cmd, timeout=DOWNLOAD_TIMEOUT, env=run_env(root, device, source))
    except subprocess.TimeoutExpired:
        return f"model download timed out after {DOWNLOAD_TIMEOUT}s"
    if r.returncode != 0:
        out = (r.stdout or "") + (r.stderr or "")
        if QUOTA in out:
            return f"model download ran out of disk at {root}: {QUOTA}."
        return "model download failed: " + last_line(out)
    return ""


def mineru_version(root: Path) -> str:
    py = venv_python(root)
    if not py.exists():
        return ""
    prog = "import importlib.metadata as m; print(m.version('mineru'))"
    try:
        r = run([py, "-c", prog], timeout=PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return ""
    return (r.stdout or "").strip() if r.returncode == 0 else ""


def setup(args) -> int:
    root = root_dir()
    device, why = pick_device(args.device)
    tier = args.tier if args.tier != "flash" else "basic"   # flash needs no models
    spec = SPEC_GPU if device == "cuda" else SPEC_CPU
    marker = read_marker(root)
    if (not args.force and is_setup(root) and marker.get("device") == device
            and marker.get("tier") == tier and marker.get("spec") == spec):
        M.log(f"mineru_local: already set up ({marker.get('mineru')}, {device}, tier {tier}) at {root}")
        return M.EXIT_OK
    if not uv_ok():
        M.log("mineru_local: uv is not on PATH; it is what builds the venv")
        return M.EXIT_NETWORK
    have = free_gb(root)
    if have < SETUP_GB:
        M.log(f"mineru_local: {root} has {have:.1f} GB free and the install needs about "
              f"{SETUP_GB:.0f} GB. Point LITREV_MINERU_HOME at a bigger filesystem.")
        return M.EXIT_NETWORK
    root.mkdir(parents=True, exist_ok=True)
    (root / "tmp").mkdir(exist_ok=True)

    if not venv_python(root).exists():
        M.log(f"mineru_local: creating the venv at {root / 'venv'} (python {PYTHON})")
        try:
            r = run(["uv", "venv", "--python", PYTHON, root / "venv"], timeout=INSTALL_TIMEOUT)
        except subprocess.TimeoutExpired:
            M.log("mineru_local: uv venv timed out")
            return M.EXIT_NETWORK
        if r.returncode != 0:
            M.log("mineru_local: uv venv failed: " + last_line((r.stdout or "") + (r.stderr or "")))
            return M.EXIT_NETWORK

    # A CUDA 12 driver cannot run the CUDA 13 torch that PyPI serves. Putting the matching
    # build in first means `mineru[torch]` finds its requirement met and downloads no second
    # set of CUDA libraries; doing it afterwards leaves both sets in the venv, 4 GB of waste.
    if device == "cuda" and driver_cuda_major() == 12:
        M.log(f"mineru_local: the driver speaks CUDA 12, so torch comes from {TORCH_INDEX}")
        msg = install_torch_for_gpu(root)
        if msg:
            M.log("mineru_local: " + msg + " (carrying on with the default torch)")

    M.log(f"mineru_local: installing {spec} (this downloads gigabytes once)")
    msg = install(root, spec)
    if msg:
        M.log("mineru_local: " + msg)
        return M.EXIT_NETWORK

    torch_v, cuda_ok = ("", False)
    if device == "cuda":
        torch_v, cuda_ok = torch_state(root)
        # Also when the probe found no torch at all: with the [torch] extra installed that
        # means the import itself raised, and a reinstall from the CUDA index is the repair.
        if not cuda_ok:
            M.log(f"mineru_local: the venv's torch ({torch_v or 'not importable'}) cannot use the "
                  f"GPU; reinstalling it from {TORCH_INDEX}")
            msg = install_torch_for_gpu(root)
            if msg:
                M.log("mineru_local: " + msg)
            torch_v, cuda_ok = torch_state(root)
        if not cuda_ok:
            # Honest downgrade: the models would run on the CPU anyway, so say so and set up onnx.
            M.log("mineru_local: no usable CUDA torch in the venv; setting up the cpu backend instead")
            device = "cpu"

    write_config(root, args.source)
    M.log(f"mineru_local: downloading the {tier} models for the {device} backend")
    msg = download_models(root, device, tier, args.source)
    if msg:
        M.log("mineru_local: " + msg)
        return M.EXIT_NETWORK

    version = mineru_version(root)
    marker_path(root).write_text(json.dumps({
        "mineru": version, "spec": spec, "device": device, "tier": tier,
        "source": args.source, "torch": torch_v, "cuda": cuda_ok,
        "python": PYTHON, "date": M.today(), "root": str(root),
    }, indent=1) + "\n")
    print(f"setup: mineru={version} device={device} tier={tier} root={root}", flush=True)
    return M.EXIT_OK


# ---------------------------------------------------------------- check

def check(args) -> int:
    root = root_dir()
    marker = read_marker(root)
    device, why = pick_device(args.device)
    print(f"root: {root}")
    print(f"disk free: {free_gb(root):.1f} GB")
    cap = mem_limit_gb()
    print(f"memory cap: {('%.1f GB' % cap) if cap else 'none readable'}")
    if cap and cap < RUN_GB:
        print(f"warning: one conversion peaked at 4.5 GB resident, above this node's "
              f"{cap:.1f} GB cap; run it on a compute node, not here")
    print(f"venv: {'present' if venv_python(root).exists() else 'missing'}")
    version = mineru_version(root)
    print(f"mineru: {version or 'not installed'}")
    torch_v, cuda_ok = torch_state(root)
    print(f"torch: {torch_v or 'absent'}" + (f", cuda available: {cuda_ok}" if torch_v else ""))
    print(f"device: {device} ({why})")
    if device == "cuda" and torch_v and not cuda_ok:
        print("warning: nvidia-smi lists a GPU but the venv's torch cannot use it; "
              "--setup would reinstall torch from " + TORCH_INDEX)
    if models_dir(root).is_dir():
        for d in sorted(models_dir(root).iterdir()):
            if not d.name.startswith("."):      # the hub keeps its .locks beside the repos
                print(f"model: {d.name}")
    if marker:
        print(f"setup: {marker.get('date')} device={marker.get('device')} "
              f"tier={marker.get('tier')} spec={marker.get('spec')}")
    if not is_setup(root):
        print("setup: incomplete — run: uv run scripts/mineru_local.py --setup")
        return M.EXIT_NETWORK
    return M.EXIT_OK


# ---------------------------------------------------------------- convert

def unpack(zip_path: Path, out: Path, pid: str):
    """markdown.md -> <out>/<pid>.md, images/ -> <out>/images/. Returns the md or None.

    MinerU's zip also holds middle_json, structured_content and model_output; the skill's
    layout has no place for them and they are several MB a paper, so they are dropped.
    """
    out.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(str(zip_path)) as z:
        names = [n for n in z.namelist() if not n.endswith("/")]
        md_name = "markdown.md" if "markdown.md" in names else next(
            (n for n in names if n.endswith(".md")), None)
        if md_name is None:
            return None
        target = out / f"{pid}.md"
        target.write_bytes(z.read(md_name))
        images = [n for n in names if n.split("/")[0] == "images"]
        if images:
            shutil.rmtree(str(out / "images"), ignore_errors=True)   # a re-run keeps no stale figure
            base = out.resolve()
            for n in images:
                dest = (out / n).resolve()
                if not str(dest).startswith(str(base) + os.sep):
                    continue                                        # a zip entry may not escape out/
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(z.read(n))
    return target


def convert(pdf: Path, out: Path, pid: str, device: str = "auto", tier: str = "basic",
            source: str = "huggingface", timeout: float = CONVERT_TIMEOUT, root=None) -> dict:
    """One local conversion. Returns {'status': 'md'|'failed', 'error': str}."""
    root = root or root_dir()
    if not is_setup(root):
        return {"status": "failed", "error": "local mineru is not set up"}
    if not Path(pdf).is_file():
        return {"status": "failed", "error": f"local mineru: no pdf at {pdf}"}
    device, why = pick_device(device)
    M.log(f"mineru_local: {pid}: {device} ({why}), tier {tier}")
    tmp = Path(tempfile.mkdtemp(dir=str(root / "tmp") if (root / "tmp").is_dir() else None,
                                prefix="conv-"))
    cmd = [cli(root), "parse", str(Path(pdf).resolve()), "-o", str(tmp), "-f", "zip",
           "--tier", tier, "--pages", "all"]
    try:
        # One at a time on this machine: a conversion holds a GPU and about 4.5 GB of RAM.
        with M.host_gate("mineru-local"):
            r = run(cmd, timeout=timeout, env=run_env(root, device, source))
        zips = sorted(tmp.glob("*.zip"))
        if not zips:
            err = last_line((r.stderr or "") + "\n" + (r.stdout or "")) or f"exit code {r.returncode}"
            return {"status": "failed", "error": "local mineru: " + err}
        md = unpack(zips[0], Path(out), pid)
        if md is None or md.stat().st_size < MIN_MD_BYTES:
            return {"status": "failed", "error": "local mineru: no usable markdown in the output zip"}
        # The standard tier's llama.cpp engine segfaults on teardown after writing its output,
        # so the markdown on disk decides the run, not the exit code.
        if r.returncode != 0:
            M.log(f"mineru_local: {pid}: markdown written although the CLI exited {r.returncode}")
        return {"status": "md", "error": ""}
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": f"local mineru: timeout after {int(timeout)}s"}
    except OSError as e:
        return {"status": "failed", "error": f"local mineru: {e}"}
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)


# ---------------------------------------------------------------- main

def main() -> int:
    p = argparse.ArgumentParser(description="Set up and run a local MinerU, with no hosted API.")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--setup", action="store_true", help="install mineru and download its models")
    mode.add_argument("--check", action="store_true", help="report what is installed and which device would be used")
    mode.add_argument("--convert", metavar="PDF", help="convert one pdf to <out>/<id>.md")
    p.add_argument("--out", help="output directory for --convert (the paper's own md/<id>/ dir)")
    p.add_argument("--id", help="paper id; default the pdf's filename without .pdf")
    p.add_argument("--device", choices=("auto", "cuda", "cpu"), default="auto",
                   help="auto = cuda when nvidia-smi lists a GPU (default auto)")
    p.add_argument("--tier", choices=TIERS, default="basic",
                   help="MinerU parse tier; basic = small models only, standard adds a 1.2 GB VLM (default basic)")
    p.add_argument("--source", choices=SOURCES, default="huggingface",
                   help="model source (default huggingface; modelscope is the mirror)")
    p.add_argument("--timeout", type=float, default=CONVERT_TIMEOUT,
                   help=f"per-pdf timeout in seconds (default {CONVERT_TIMEOUT})")
    p.add_argument("--force", action="store_true", help="--setup: reinstall and re-download even when set up")
    args = p.parse_args()

    if args.setup:
        return setup(args)
    if args.check:
        return check(args)
    if not args.out:
        M.log("mineru_local: --convert needs --out")
        return M.EXIT_USAGE
    pdf = Path(args.convert).expanduser()
    pid = args.id or pdf.stem
    if not is_setup():
        M.log(SETUP_MSG)
        return M.EXIT_NETWORK
    res = convert(pdf, Path(args.out), pid, args.device, args.tier, args.source, args.timeout)
    if res["status"] != "md":
        M.log("mineru_local: " + res["error"])
        return M.EXIT_NETWORK
    print(f"md={Path(args.out) / (pid + '.md')}", flush=True)
    return M.EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
