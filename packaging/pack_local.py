"""打 Windows 本地部署包：exe 版 + 源码版。"""
import hashlib
import pathlib
import shutil
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
VER = "1.0.0"

EXE = DIST / "wb-checkin.exe"
if not EXE.exists():
    raise SystemExit("找不到 dist/wb-checkin.exe，先跑 PyInstaller")

SILENT_BAT = """@echo off
cd /d "%~dp0"
set WB_NO_BROWSER=1
echo 正在启动 WorkBuddy 签到助手（不自动打开浏览器）……
echo 控制台地址见下面的「控制台: http://...」一行，把它粘到浏览器打开。
echo 关闭本窗口即停止服务。
echo.
wb-checkin.exe
echo.
pause
"""

readme = (ROOT / "packaging" / "使用说明.txt").read_text(encoding="utf-8")
readme = "\r\n".join(readme.replace("\r\n", "\n").split("\n"))


def crlf(text):
    return "\r\n".join(text.replace("\r\n", "\n").split("\n"))


def zip_dir(src: pathlib.Path, out: pathlib.Path):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for p in sorted(src.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(src.parent))
    return out


def sha256(p: pathlib.Path):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- 1. exe 版 ----------
exe_pkg = DIST / ("wb-checkin-%s-win64" % VER)
if exe_pkg.exists():
    shutil.rmtree(exe_pkg)
exe_pkg.mkdir(parents=True)

shutil.copy2(EXE, exe_pkg / "wb-checkin.exe")
(exe_pkg / "静默启动.bat").write_bytes(crlf(SILENT_BAT).encode("gbk"))
(exe_pkg / "使用说明.txt").write_bytes(readme.encode("utf-8-sig"))

exe_zip = DIST / ("wb-checkin-%s-win64.zip" % VER)
zip_dir(exe_pkg, exe_zip)

# ---------- 2. 源码版 ----------
src_pkg = DIST / ("wb-checkin-%s-source" % VER)
if src_pkg.exists():
    shutil.rmtree(src_pkg)
src_pkg.mkdir(parents=True)

for name in ("app.py", "requirements.txt", "start.bat", "README.md", ".env.example", "wb.ico"):
    s = ROOT / name
    if s.exists():
        shutil.copy2(s, src_pkg / name)
shutil.copytree(ROOT / "static", src_pkg / "static")
(src_pkg / "本地运行说明.txt").write_bytes(readme.encode("utf-8-sig"))

src_zip = DIST / ("wb-checkin-%s-source.zip" % VER)
zip_dir(src_pkg, src_zip)

# ---------- 报告 ----------
print("=" * 56)
for f in (EXE, exe_zip, src_zip):
    mb = f.stat().st_size / 1024 / 1024
    print("%-34s %6.2f MB  %s" % (f.name, mb, sha256(f)[:16]))
print("=" * 56)
for z in (exe_zip, src_zip):
    with zipfile.ZipFile(z) as zf:
        print("\n[%s]" % z.name)
        for i in zf.infolist():
            print("   %8d  %s" % (i.file_size, i.filename))
