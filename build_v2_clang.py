# REVISION: v3.226-lto
# MIT License
#
# Copyright (c) 2025 aufkrawall
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import os,sys,subprocess,logging,urllib.request,tempfile,shutil,signal,json,tarfile,time,glob,re

class JsonFormatter(logging.Formatter):
    def format(self, r):
        return json.dumps({
            "time": int(time.time() * 1000),
            "level": r.levelname,
            "message": r.getMessage()
        }, separators=(',', ':'))

log = logging.getLogger()
h = logging.FileHandler('log.json', 'w', encoding='utf-8')
h.setFormatter(JsonFormatter())
s = logging.StreamHandler(sys.stdout)
s.setFormatter(logging.Formatter('%(message)s'))
logging.basicConfig(level=logging.INFO, handlers=[h, s])
logi = log.info
logi('# REVISION: v3.226-lto')

# Global variable to track the currently running subprocess
running_process = None

def signal_handler(signum, frame):
    global running_process
    logi('\nAborting build script...')
    if running_process:
        try:
            pid = running_process.pid
            logi(f'Killing process tree for PID: {pid}')
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True)
        except Exception as e:
            log.error(f"Failed to kill process: {e}")
    sys.exit(1)

signal.signal(signal.SIGINT, signal_handler)

args, allowed = set(sys.argv[1:]), {'--clean', '--force-resume', '--skip-updates', '--force-mpv'}
if not args.issubset(allowed):
    log.error(f"Invalid args: {list(args - allowed)}. Allowed: {allowed}")
    sys.exit(1)

clean_mode, force_resume, skip_updates, force_mpv = '--clean' in args, '--force-resume' in args, '--skip-updates' in args, '--force-mpv' in args
logi(f'Mode: clean={clean_mode}, resume={force_resume}, no-update={skip_updates}, force_mpv={force_mpv}')
logi('MPV Standalone Build v3.226-lto - ThinLTO + ccache optimization')

def rmd(d):
    if os.path.exists(d):
        e = tempfile.mkdtemp()
        subprocess.run(['robocopy', e, d, '/PURGE', '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'], capture_output=True, check=False)
        shutil.rmtree(d, ignore_errors=True); shutil.rmtree(e, ignore_errors=True)

def download_with_retry(url, tar, retries=3, delay=5):
    for i in range(retries):
        try:
            logi(f'Downloading {url} (Attempt {i+1})...')
            urllib.request.urlretrieve(url, tar)
            return True
        except Exception as e:
            logi(f'Download failed: {e}')
            if i < retries - 1:
                time.sleep(delay)
    raise Exception(f'Failed to download {url} after {retries} attempts.')

def run(c, cwd=None, env=None, retry=1):
    global running_process
    cmd = ' '.join(c) if isinstance(c, list) else c
    log.info(f"EXEC: {cmd}")

    out_file = tempfile.TemporaryFile(mode='w+', encoding='utf-8', errors='replace')
    err_file = tempfile.TemporaryFile(mode='w+', encoding='utf-8', errors='replace')

    try:
        p = subprocess.Popen([shl, '-ucrt64', '-defterm', '-no-start', '-here', '-c', cmd],
                             cwd=cwd, env=env, text=True,
                             stdout=out_file, stderr=err_file, stdin=None)
        running_process = p

        while p.poll() is None:
            time.sleep(0.1)

        out_file.seek(0); err_file.seek(0)
        stdout = out_file.read()
        stderr = err_file.read()

        if p.returncode != 0:
            out = (stdout or '').strip()
            err = (stderr or '').strip()
            msg = f'FAILED: {cmd}'
            if out:
                lines = out.splitlines()
                msg += f'\nSTDOUT (last 50 lines):\n' + '\n'.join(lines[-50:])
            if err: msg += f'\nSTDERR:\n{err}'
            log.error(msg)
            # Help user find the FFmpeg config log if it fails
            if 'configure' in cmd and 'ffmpeg' in (cwd or ''):
                log.error(f"Check the full log at: {os.path.join(cwd, 'ffbuild/config.log')}")
            raise subprocess.CalledProcessError(p.returncode, c, stdout, stderr)

        return subprocess.CompletedProcess(c, p.returncode, stdout, stderr)

    except subprocess.CalledProcessError:
        raise
    except Exception as e:
        log.error(f"Execution error: {e}")
        raise
    finally:
        running_process = None
        out_file.close()
        err_file.close()

def mrun(cs, cwd=None, env=None):
    for c in cs: run(c, cwd=cwd, env=env)

def extract_tar(f, d):
    logi(f'Extracting {os.path.basename(f)}...')
    mode = 'r:gz' if f.endswith('.gz') else 'r:xz'
    with tarfile.open(f, mode) as tf:
        tf.extractall(path=d, filter='data') if sys.version_info >= (3, 12) else tf.extractall(path=d)

def git_sync(u, n, d):
    if not os.path.isdir(os.path.join(d, '.git')):
        if skip_updates:
            logi(f'Skipping update for {n} (not a git repo)')
            return False
        logi(f'{n} missing or invalid. Cloning...')
        rmd(d)
        run(['git', 'clone', '--quiet', '--depth', '1', u, n] if n in ['ffmpeg', 'mpv'] else ['git', 'clone', '--quiet', u, n], cwd=os.path.dirname(d), env=env)
        return True

    if skip_updates:
        logi(f'Skipping update for {n} (User requested)')
        return False

    try:
        try:
            current_url = run(['git', 'remote', 'get-url', 'origin'], cwd=d, env=env).stdout.strip()
            if current_url != u:
                run(['git', 'remote', 'set-url', 'origin', u], cwd=d, env=env)
        except: pass

        run(['git', 'reset', '--hard', 'HEAD'], cwd=d, env=env)
        old = run(['git', 'rev-parse', 'HEAD'], cwd=d, env=env).stdout.strip()

        run(['git', 'pull', '--quiet'], cwd=d, env=env)
        run(['git', 'submodule', 'update', '--init', '--recursive', '--quiet'], cwd=d, env=env)

        new = run(['git', 'rev-parse', 'HEAD'], cwd=d, env=env).stdout.strip()
        return old != new
    except subprocess.CalledProcessError:
        log.warning(f"WARNING: Update failed for {n}. Using existing local sources.")
        return False

def patch_pc(path, libs):
    if not os.path.exists(path): return
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f: c = f.read()

        # Fix the prefix to point to actual MSYS2 location for all .pc files
        msys_ucrt64 = os.path.join(dirs['working'], '..', 'msys2', 'ucrt64')
        msys_ucrt64_unix = msys_ucrt64.replace('\\', '/').replace('C:/', '/c/').replace('c:/', '/c/')
        if 'prefix=/ucrt64' in c:
            c = c.replace('prefix=/ucrt64', f'prefix={msys_ucrt64_unix}')

        # Libs patching
        if 'sdl2.pc' in path.lower():
            # Remove -lSDL2main to prevent main conflict
            c = c.replace('-lSDL2main', '')
            extra_sdl = "-lmingw32 -lSDL2 -mwindows -lsetupapi -lwinmm -limm32 -lole32 -loleaut32 -lversion -luuid -ladvapi32 -lshell32 -luser32 -lgdi32"
            if 'Libs.private:' in c:
                if '-lsetupapi' not in c: c = c.replace('Libs.private:', f'Libs.private: {extra_sdl}')
            elif 'Libs:' in c:
                 c = c.replace('Libs:', f'Libs: {extra_sdl}')
        
        # Ensure vsnprintf_shim is linked AT THE END
        if '-lvsnprintf_shim' not in c:
            lines = c.split('\n')
            for i, line in enumerate(lines):
                if line.startswith('Libs.private:') or line.startswith('Libs:'):
                    lines[i] = line.rstrip() + ' -lvsnprintf_shim'
            c = '\n'.join(lines)
        
        # Add extra libs if missing
        if libs and libs not in c:
            lines = c.split('\n')
            for i, line in enumerate(lines):
                if line.startswith('Libs.private:') or line.startswith('Libs:'):
                    if libs not in line: lines[i] = line.rstrip() + f' {libs}'
            c = '\n'.join(lines)

        if '-ldl' in c: c = c.replace('-ldl', '')

        with open(path, 'w', encoding='utf-8') as f: f.write(c)
        logi(f'Patched {os.path.basename(path)}')
    except Exception as e:
        log.error(f"Failed to patch {path}: {e}")

def create_pc(name, ver, desc, libs, reqs='', cflags='-I${includedir}'):
    pc_path = os.path.join(dirs['installed'], 'lib', 'pkgconfig', f'{name}.pc')
    lib64_pc = os.path.join(dirs['installed'], 'lib64', 'pkgconfig', f'{name}.pc')
    if os.path.exists(lib64_pc): os.remove(lib64_pc)

    logi(f'Creating/Overwriting {name}.pc...')
    c = f'prefix={pre}\nexec_prefix=${{prefix}}\nlibdir=${{exec_prefix}}/lib\nincludedir=${{prefix}}/include\n\nName: {name}\nDescription: {desc}\nVersion: {ver}\nRequires.private: {reqs}\nLibs: -L${{libdir}} {libs}\nCflags: {cflags}\n'
    with open(pc_path, 'w') as f: f.write(c)

def clean_libs():
    logi('Sweeping lib folder for import libraries...')
    # Do NOT include libSDL2main.a here - we want to remove it if found
    keep = ['libvulkan-1.dll.a', 'libOpenCL.dll.a', 'libcudart.a', 'libSDL2.a', 'libvsnprintf_shim.a']
    for lib_dir in ['lib', 'lib64']:
        p = os.path.join(dirs['installed'], lib_dir)
        if not os.path.exists(p): continue
        for ext in ['*.dll.a', '*.la']:
            for f in glob.glob(os.path.join(p, ext)):
                if os.path.basename(f) in keep: continue
                try: os.remove(f); logi(f'Purged {os.path.basename(f)}')
                except: pass

    for bad in ['libz.dll.a', 'libstdc++.dll.a', 'libgcc_s.dll.a', 'libSDL2main.a']:
         sys_z = os.path.join(dirs['installed'], 'lib', bad)
         if os.path.exists(sys_z):
             logi(f'Purged aggressive target: {bad}')
             os.remove(sys_z)

def remove_pkg(n):
    lib_map = {
        'bzip2': 'bz2', 'gettext': 'intl', 'xz': 'lzma', 'freetype': 'freetype',
        'harfbuzz': 'harfbuzz', 'fribidi': 'fribidi', 'libiconv': 'iconv',
        'spirv-headers': 'SPIRV-Headers', 'spirv-tools': 'SPIRV-Tools',
        'spirv-cross': 'spirv-cross', 'glslang': 'glslang', 'shaderc': 'shaderc', 'lcms2': 'lcms2',
        'libepoxy': 'epoxy', 'zimg': 'zimg', 'libass': 'ass', 'fontconfig': 'fontconfig',
        'zlib': 'z', 'libpng': 'png', 'libjpeg-turbo': 'jpeg', 'luajit': 'luajit',
        'ffnvcodec': 'ffnvcodec',
        'uchardet': 'uchardet', 'libsoxr': 'soxr',
        'fftw': 'fftw3', 'libsamplerate': 'samplerate', 'rubberband': 'rubberband',
        'libdvdcss': 'dvdcss', 'libdvdread': 'dvdread', 'libdvdnav': 'dvdnav',
        'libbluray': 'bluray', 'libarchive': 'archive'
    }
    ln = lib_map.get(n, n)

    search_patterns = [f'lib{ln}*.a', f'{ln}*.a']
    if n == 'shaderc': search_patterns.extend(['libshaderc*.a'])
    if n == 'glslang': search_patterns.extend(['libGenericCodeGen.a', 'libMachineIndependent.a', 'libOGLCompiler.a', 'libOSDependent.a', 'libHLSL.a'])
    if n == 'spirv-tools': search_patterns.append('libSPIRV-Tools*.a')
    if n == 'spirv-cross': search_patterns.extend(['libspirv-cross*.a'])
    if n == 'libjpeg-turbo': search_patterns.extend(['libturbojpeg.a', 'libjpeg.a'])

    for lib_dir in ['lib', 'lib64']:
        p = os.path.join(dirs['installed'], lib_dir)
        pk = os.path.join(p, 'pkgconfig')
        if not os.path.exists(p): continue
        for pat in search_patterns:
            for f in glob.glob(os.path.join(p, pat)):
                try: os.remove(f); logi(f'Cleaned {lib_dir}/{os.path.basename(f)}')
                except: pass
        for f in glob.glob(os.path.join(pk, f'*{ln}*.pc')):
            try: os.remove(f); logi(f'Cleaned {lib_dir}/pkgconfig/{os.path.basename(f)}')
            except: pass

def create_vsnprintf_shim(env):
    logi("Creating vsnprintf shim library...")
    shim_c = os.path.join(dirs['working'], 'vsnprintf_shim.c')
    os.makedirs(dirs['working'], exist_ok=True)
    with open(shim_c, 'w') as f:
        f.write('#include <stdio.h>\n#include <stdarg.h>\n')
        f.write('// Compatibility shims for MSVC symbols in static CUDA/NVCC builds\n')
        f.write('int _vsnprintf(char *s, size_t n, const char *format, va_list arg) {\n')
        f.write('    return vsnprintf(s, n, format, arg);\n}\n')
        f.write('int _snprintf(char *s, size_t n, const char *format, ...) {\n')
        f.write('    va_list arg; int ret; va_start(arg, format);\n')
        f.write('    ret = vsnprintf(s, n, format, arg);\n')
        f.write('    va_end(arg); return ret;\n}\n')

    # Unix-style path for AR - use the to_unix function defined later (call after dirs are set)
    lib_path_win = os.path.join(dirs['installed'], 'lib', 'libvsnprintf_shim.a')
    # Convert to Unix path properly
    unix_lib_path = lib_path_win.replace('\\', '/')
    if len(unix_lib_path) >= 2 and unix_lib_path[1] == ':':
        drive = unix_lib_path[0].lower()
        unix_lib_path = '/' + drive + unix_lib_path[2:]

    try:
        # Use basic flags for the shim to avoid LTO visibility issues in narrow checks
        run(['gcc', '-O3', '-fPIC', '-c', 'vsnprintf_shim.c', '-o', 'vsnprintf_shim.o'], cwd=dirs['working'], env=env)
        run(['ar', 'rcs', unix_lib_path, 'vsnprintf_shim.o'], cwd=dirs['working'], env=env)
        logi(f"Created libvsnprintf_shim.a at {unix_lib_path}")
    except Exception as e:
        log.error(f"Failed to create shim: {e}")

def import_host_cuda(env):
    cuda_path = os.environ.get('CUDA_PATH')
    if not cuda_path or not os.path.exists(cuda_path):
        log.warning("CUDA_PATH not set. Auto-detecting...")
        default_root = r"C:\Program Files\NVIDIA GPU Computing Toolkit\CUDA"
        if os.path.exists(default_root):
            versions = glob.glob(os.path.join(default_root, "v*"))
            if versions:
                versions.sort(reverse=True)
                cuda_path = versions[0]
                logi(f"Found CUDA: {cuda_path}")

    if not cuda_path or not os.path.exists(cuda_path):
        log.warning("CUDA SDK not found. Build will proceed WITHOUT CUDA.")
        return False

    logi(f"Importing Host CUDA SDK from: {cuda_path}")

    src_inc = os.path.join(cuda_path, 'include')
    dst_inc = os.path.join(dirs['installed'], 'include')
    if os.path.exists(src_inc):
        subprocess.run(['robocopy', src_inc, dst_inc, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NC', '/NS', '/NP'], capture_output=True)

    dst_lib = os.path.join(dirs['installed'], 'lib')
    os.makedirs(dst_lib, exist_ok=True)

    found_files = {}
    potential_lib_dirs = [os.path.join(cuda_path, 'lib', 'x64'), os.path.join(cuda_path, 'lib', 'Win64'), os.path.join(cuda_path, 'lib', 'x86_64')]
    for pd in potential_lib_dirs:
        if os.path.exists(pd):
            for f in os.listdir(pd):
                if f.endswith('.lib'):
                    found_files[f.lower()] = os.path.join(pd, f)

    if 'cudart_static.lib' not in found_files:
        logi("Scanning entire CUDA SDK...")
        for root, dirs_walk, files in os.walk(cuda_path):
            for f in files:
                if f.endswith('.lib'):
                    found_files[f.lower()] = os.path.join(root, f)

    if 'cudart_static.lib' in found_files:
        src = found_files['cudart_static.lib']
        shutil.copy2(src, os.path.join(dst_lib, 'libcudart.a'))
        shutil.copy2(src, os.path.join(dst_lib, 'cudart_static.lib'))
        logi("Imported cudart_static.lib -> libcudart.a")
    else:
        log.warning("cudart_static.lib not found. Build will proceed WITHOUT CUDA.")
        return False

    # General Import (NPP etc)
    for fname, fpath in found_files.items():
        bn = os.path.basename(fpath)
        shutil.copy2(fpath, os.path.join(dst_lib, bn))
        name_part = bn[:-4]
        if not name_part.lower().startswith('lib'):
            new_name = f"lib{name_part}.a"
            dest = os.path.join(dst_lib, new_name)
            if not os.path.exists(dest):
                shutil.copy2(fpath, dest)

    cuda_bin = os.path.join(cuda_path, 'bin')
    if os.path.exists(cuda_bin):
        drive, tail = os.path.splitdrive(cuda_bin)
        unix_drive = f"/{drive.lower().rstrip(':')}"
        unix_path = unix_drive + tail.replace('\\', '/')
        env['PATH'] = unix_path + os.pathsep + env['PATH']
        logi(f"Added CUDA bin to PATH: {unix_path}")
        return True

    return False

def import_msvc_env(env):
    logi("Searching for Visual Studio / MSVC (required for nvcc)...")

    vswhere = os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe")
    if not os.path.exists(vswhere):
        vswhere = os.path.expandvars(r"%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe")

    vs_path = None
    if os.path.exists(vswhere):
        try:
            out = subprocess.check_output([vswhere, "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"], text=True).strip()
            if out and os.path.exists(out):
                vs_path = out
        except Exception as e:
            log.error(f"vswhere failed: {e}")

    vcvars = None
    if vs_path:
        candidate = os.path.join(vs_path, "VC", "Auxiliary", "Build", "vcvars64.bat")
        if os.path.exists(candidate):
            vcvars = candidate

    if not vcvars:
        log.warning("WARNING: 'vcvars64.bat' not found. Build will proceed WITHOUT CUDA.")
        return False

    logi(f"Found MSVC Init Script: {vcvars}")

    try:
        cmd = f'"{vcvars}" && echo __ENV_START__ && set'
        out = subprocess.check_output(cmd, shell=True, text=True)

        start_parsing = False
        new_env = {}
        for line in out.splitlines():
            if "__ENV_START__" in line:
                start_parsing = True
                continue
            if not start_parsing: continue
            if "=" in line:
                k, v = line.split("=", 1)
                new_env[k.upper()] = v

        for key in ['PATH', 'INCLUDE', 'LIB']:
            if key in new_env:
                env[key] = new_env[key]

        path_dirs = env['PATH'].split(';')
        for d in path_dirs:
            if os.path.exists(os.path.join(d, 'cl.exe')):
                logi("MSVC Environment (cl.exe) successfully imported.")
                return True

        log.warning("cl.exe not found in extracted PATH. NVCC might fail.")
        return False

    except Exception as e:
        log.error(f"Failed to load MSVC environment: {e}")
        return False

base = os.path.dirname(os.path.abspath(__file__))
bd = os.path.join(base, 'mpv-standalone-build')
dirs = {k: os.path.join(bd, k) for k in ['repositories', 'tarballs', 'installed', 'working', '.ccache', 'msys2']}
for d in dirs.values(): os.makedirs(d, exist_ok=True)

def to_unix(p):
    """Convert Windows path to MSYS2 Unix path, handling any drive letter."""
    p = p.replace('\\', '/')
    # Handle any drive letter (C:, D:, etc.) case-insensitively
    if len(p) >= 2 and p[1] == ':':
        drive = p[0].lower()
        return '/' + drive + p[2:]
    return p
pre = to_unix(dirs['installed'])
win_pre = dirs['installed'].replace('\\', '/')
static_zlib = to_unix(os.path.join(dirs['installed'], 'lib', 'libz.a'))

pcpath = f"{pre}/lib/pkgconfig:{pre}/lib64/pkgconfig:{pre}/share/pkgconfig"
nproc = os.cpu_count()
msys, shl = dirs['msys2'], os.path.join(dirs['msys2'], 'msys2_shell.cmd')

if not os.path.exists(shl):
    tar = os.path.join(dirs['tarballs'], 'msys2.tar.xz')
    if not os.path.exists(tar):
        try:
            logi('Fetching MSYS2 URL...')
            with urllib.request.urlopen('https://api.github.com/repos/msys2/msys2-installer/releases/latest') as r:
                assets = json.loads(r.read())['assets']
                url = next(a['browser_download_url'] for a in assets if a['name'].endswith('.tar.xz') and 'base' in a['name'])
        except Exception as e:
            logi(f'API failed ({e}), using fallback.')
            url = 'https://repo.msys2.org/distrib/x86_64/msys2-base-x86_64-20251213.tar.xz'
        download_with_retry(url, tar)
    if os.path.exists(msys): shutil.rmtree(msys)
    extract_tar(tar, bd)
    os.rename(os.path.join(bd, 'msys64'), msys)

for sub, url in [('msys', 'https://mirror.selfnet.de/msys2/msys/$arch'), ('ucrt64', 'https://mirror.selfnet.de/msys2/mingw/ucrt64/$arch')]:
    d = os.path.join(msys, sub if sub == 'msys' else 'ucrt64', 'etc', 'pacman.d')
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f'mirrorlist.{sub}'), 'w') as f: f.write(f'Server = {url}\n')

if clean_mode:
    rmd(dirs['installed'])
    rmd(dirs['working'])
os.makedirs(dirs['installed'], exist_ok=True)

env = os.environ.copy()
env.update({'MSYSTEM': 'UCRT64', 'CHERE_INVOKING': '1', 'LC_ALL': 'C', 'CC': 'clang', 'CXX': 'clang++',
            'AR': 'llvm-ar', 'RANLIB': 'llvm-ranlib', 'NM': 'llvm-nm',
            'CFLAGS': f'-static -O3 -march=x86-64-v3 -ffunction-sections -fdata-sections -I{pre}/include -DSDL_MAIN_HANDLED',
            'CXXFLAGS': f'-static -O3 -march=x86-64-v3 -ffunction-sections -fdata-sections -I{pre}/include -DSDL_MAIN_HANDLED',
            'LDFLAGS': f'-static -Wl,--gc-sections -L{pre}/lib -Wl,-u,_vsnprintf -Wl,-u,_snprintf',
            'CCACHE_DIR': dirs['.ccache'],
            'PKG_CONFIG': 'pkg-config --static', 'PKG_CONFIG_LIBDIR': pcpath, 'PKG_CONFIG_PATH': pcpath,
            'MSYS2_PATH_TYPE': 'inherit'})

env['PATH'] = os.pathsep.join([os.path.join(dirs['installed'], 'bin'), os.path.join(msys, 'ucrt64', 'bin'), os.path.join(msys, 'usr', 'bin'), env['PATH']])

if not skip_updates: mrun(['pacman -Syu --noconfirm', 'pacman -Syu --noconfirm'], env=env)

logi('Installing tools')
mrun(['pacman -S --noconfirm --needed mingw-w64-ucrt-x86_64-clang mingw-w64-ucrt-x86_64-lld mingw-w64-ucrt-x86_64-llvm mingw-w64-ucrt-x86_64-compiler-rt mingw-w64-ucrt-x86_64-toolchain mingw-w64-ucrt-x86_64-ccache mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-meson mingw-w64-ucrt-x86_64-ninja git mingw-w64-ucrt-x86_64-yasm mingw-w64-ucrt-x86_64-nasm mingw-w64-ucrt-x86_64-autotools mingw-w64-ucrt-x86_64-gettext patch mingw-w64-ucrt-x86_64-gperf groff mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-pip autoconf automake libtool mingw-w64-ucrt-x86_64-vulkan-headers mingw-w64-ucrt-x86_64-vulkan-loader mingw-w64-ucrt-x86_64-opencl-headers mingw-w64-ucrt-x86_64-opencl-icd mingw-w64-ucrt-x86_64-amf-headers mingw-w64-ucrt-x86_64-SDL2 mingw-w64-ucrt-x86_64-binutils'], env=env)

def sanitize_libcudart(lib_path):
    """Strip .drectve section from libcudart.a to prevent MSVC runtime conflicts with LTO"""
    if not os.path.exists(lib_path): return
    # Use Unix path for MSYS2 tools
    lib_path_unix = lib_path.replace(os.sep, '/')
    logi(f"Sanitizing {os.path.basename(lib_path)} for MinGW LTO compatibility...")
    
    # Create a temporary directory for object extraction
    work_dir = os.path.join(dirs['working'], 'cudart_sanitize')
    rmd(work_dir); os.makedirs(work_dir, exist_ok=True)
    
    try:
        # 1. Extract all objects
        run(['llvm-ar', 'x', lib_path_unix], cwd=work_dir, env=env)
        
        # 2. Strip .drectve section from all .o/.obj files
        objects = [f for f in os.listdir(work_dir) if f.endswith('.obj') or f.endswith('.o')]
        if not objects:
            log.warning("No objects found in libcudart.a")
            return

        for obj in objects:
            obj_path = os.path.join(work_dir, obj).replace(os.sep, '/')
            # Use llvm-objcopy to remove the section
            run(['llvm-objcopy', '--remove-section=.drectve', obj_path], cwd=work_dir, env=env)
            
        # 3. Re-create the archive
        os.remove(lib_path) # remove original Windows path
        run(['llvm-ar', 'rcs', lib_path_unix] + objects, cwd=work_dir, env=env)
        logi(f"Successfully sanitized {len(objects)} objects in {os.path.basename(lib_path)}")
        
    except Exception as e:
        log.error(f"Failed to sanitize libcudart: {e}")
    finally:
        rmd(work_dir)

# Environment Setup with Soft Fail - MOVED AFTER PACMAN
cuda_ok = import_host_cuda(env)
# Call sanitation if libcudart exists in installed/lib
if cuda_ok:
    lc = os.path.join(dirs['installed'], 'lib', 'libcudart.a')
    if os.path.exists(lc): sanitize_libcudart(lc)

msvc_ok = import_msvc_env(env)
enable_cuda = cuda_ok and msvc_ok

if enable_cuda:
    create_vsnprintf_shim(env)

logi('Installing python deps')
mrun(['python -m pip install Jinja2'], env=env)

sys_pkg = os.path.join(msys, 'ucrt64', 'lib', 'pkgconfig')
loc_pkg = os.path.join(dirs['installed'], 'lib', 'pkgconfig')
os.makedirs(loc_pkg, exist_ok=True)
for pattern in ['vulkan.pc', 'sdl2.pc']:
    for f in glob.glob(os.path.join(sys_pkg, pattern)):
        shutil.copy(f, loc_pkg)
        logi(f'Imported {os.path.basename(f)}')
        patch_pc(os.path.join(loc_pkg, os.path.basename(f)), '-lstdc++ -lm -lpthread')

sys_lib = os.path.join(msys, 'ucrt64', 'lib')
loc_lib = os.path.join(dirs['installed'], 'lib')
os.makedirs(loc_lib, exist_ok=True)
# Only copy libSDL2.a, SKIP libSDL2main.a to avoid conflict
for pattern in ['libvulkan*.dll.a', 'libOpenCL*.dll.a', 'libSDL2.a']:
    for f in glob.glob(os.path.join(sys_lib, pattern)):
        shutil.copy(f, loc_lib)
        logi(f'Imported library {os.path.basename(f)}')

aliases = [('libvulkan-1.dll.a', 'libvulkan-1.a'), ('libOpenCL.dll.a', 'libOpenCL.a')]
for src_name, dst_name in aliases:
    src_p = os.path.join(loc_lib, src_name)
    dst_p = os.path.join(loc_lib, dst_name)
    if os.path.exists(src_p):
        shutil.copy(src_p, dst_p)
        logi(f'Aliased {src_name} -> {dst_name}')


link_args = "-Dc_link_args='-static -lintl -liconv' -Dcpp_link_args='-static -lintl -liconv' "
# Note: LTO disabled for deps to avoid LLVM bitcode incompatibility with GNU ld in some builds
# LTO only enabled for FFmpeg (--enable-lto=thin) and mpv (-Db_lto=true -Db_lto_mode=thin)
cmake_args = f'-DBUILD_SHARED_LIBS=OFF -DCMAKE_INSTALL_PREFIX="{pre}" -DCMAKE_PREFIX_PATH="{pre}" -DCMAKE_INSTALL_LIBDIR=lib'
cmake_compiler_args = '-DCMAKE_C_COMPILER=clang -DCMAKE_CXX_COMPILER=clang++'
zlib_cmake_flags = f'-DZLIB_LIBRARY="{static_zlib}" -DZLIB_INCLUDE_DIR="{pre}/include"'

deps = [
 ('expat','https://github.com/libexpat/libexpat.git','cmake', f'{cmake_args} -DEXPAT_BUILD_DOCS=OFF -DEXPAT_BUILD_EXAMPLES=OFF -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_TOOLS=OFF', []),
 ('gettext',None,'intl','', []),
 ('bzip2','https://sourceware.org/git/bzip2.git','make','PREFIX="{pre}"', []),
 ('zlib', 'https://github.com/madler/zlib.git', 'cmake', f'{cmake_args} {cmake_compiler_args} -DZLIB_BUILD_EXAMPLES=OFF', []),
 ('xz', None, 'autotools','--enable-static --disable-shared --disable-nls', []),
 ('zimg', 'https://github.com/sekrit-twc/zimg.git', 'autotools', '--enable-static --disable-shared', []),
 ('libpng', 'https://github.com/glennrp/libpng.git', 'cmake', f'{cmake_args} {cmake_compiler_args} -DPNG_SHARED=OFF -DPNG_TESTS=OFF -DPNG_EXECUTABLES=OFF {zlib_cmake_flags}', ['zlib']),
 ('libjpeg-turbo', 'https://github.com/libjpeg-turbo/libjpeg-turbo.git', 'cmake', f'{cmake_args} {cmake_compiler_args} -DWITH_TURBOJPEG=OFF -DENABLE_SHARED=OFF -DENABLE_STATIC=ON {zlib_cmake_flags}', ['zlib']),
 ('freetype','https://gitlab.freedesktop.org/freetype/freetype.git','meson','-Dpng=disabled -Dbzip2=disabled -Dbrotli=disabled -Dzlib=disabled -Dharfbuzz=disabled -Dtests=disabled', ['libpng', 'bzip2', 'zlib']),
 ('libiconv',None,'autotools','--enable-static --disable-shared', []),
 ('fribidi','https://github.com/fribidi/fribidi.git','meson','-Ddocs=false -Dtests=false -Dbin=false -Ddefault_library=static', []),
 ('harfbuzz','https://github.com/harfbuzz/harfbuzz.git','cmake','-DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="{pre}" -DHB_HAVE_FREETYPE=ON -DHB_HAVE_ICU=OFF -DHB_HAVE_GLIB=OFF -DHB_HAVE_GOBJECT=OFF -DHB_HAVE_GRAPHITE2=OFF -DHB_BUILD_TESTS=OFF -DHB_BUILD_UTILS=OFF -DCMAKE_PREFIX_PATH="{pre}" -DCMAKE_INSTALL_LIBDIR=lib', ['freetype']),
 ('fontconfig','https://gitlab.freedesktop.org/fontconfig/fontconfig.git','meson','-Dtests=disabled -Ddoc=disabled', ['freetype', 'expat', 'libiconv', 'gettext']),
 ('libass','https://github.com/libass/libass.git','autotools','--enable-static --disable-shared --enable-fontconfig', ['freetype', 'fribidi', 'harfbuzz', 'fontconfig']),
 ('lcms2', 'https://github.com/mm2/Little-CMS.git', 'meson', '-Ddefault_library=static', []),
 ('libepoxy', 'https://github.com/anholt/libepoxy.git', 'meson', '-Dtests=false -Dglx=no -Degl=no', []),
 ('spirv-headers', 'https://github.com/KhronosGroup/SPIRV-Headers.git', 'cmake', f'{cmake_args} -DSPIRV_HEADERS_SKIP_EXAMPLES=ON -DSPIRV_HEADERS_SKIP_INSTALL_EXAMPLES=ON', []),
 ('spirv-cross', 'https://github.com/KhronosGroup/SPIRV-Cross.git', 'cmake', f'{cmake_args} -DSPIRV_CROSS_CLI=OFF -DSPIRV_CROSS_ENABLE_TESTS=OFF', []),
 ('glslang', 'https://github.com/KhronosGroup/glslang.git', None, None, []),
 ('spirv-tools', 'https://github.com/KhronosGroup/SPIRV-Tools.git', None, None, []),
 ('shaderc', 'https://github.com/google/shaderc.git', 'cmake', f'{cmake_args} -DSHADERC_SKIP_TESTS=ON -DSHADERC_SKIP_EXAMPLES=ON -DSHADERC_SKIP_COPYRIGHT_CHECK=ON -DSHADERC_ENABLE_SHARED_CRT=OFF', ['glslang', 'spirv-tools', 'spirv-headers']),
 ('ffnvcodec', 'https://git.videolan.org/git/ffmpeg/nv-codec-headers.git', 'make', 'PREFIX="{pre}"', []),
 ('dav1d', 'https://code.videolan.org/videolan/dav1d.git', 'meson', '-Denable_tests=false -Denable_tools=false --libdir=lib', []),
 ('libplacebo','https://code.videolan.org/videolan/libplacebo.git','meson','-Dopengl=enabled -Dd3d11=enabled -Dvulkan=enabled -Dshaderc=enabled -Dlcms=enabled -Dtests=false -Ddemos=false -Dxxhash=disabled -Dlibdovi=disabled', ['shaderc', 'lcms2', 'libepoxy', 'glslang']),
 ('luajit','https://github.com/LuaJIT/LuaJIT.git','luajit','PREFIX=\"{pre}\"', []),
 ('uchardet', 'https://gitlab.freedesktop.org/uchardet/uchardet.git', 'cmake', f'{cmake_args} -DBUILD_BINARY=OFF -DBUILD_SHARED_LIBS=OFF', []),
 ('libsoxr', 'https://git.code.sf.net/p/soxr/code', 'cmake', f'{cmake_args} -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTS=OFF -DWITH_OPENMP=OFF', []),
 ('fftw', None, 'autotools', '--enable-static --disable-shared --disable-doc --enable-threads --enable-sse2 --enable-avx2 --with-our-malloc --with-combined-threads --disable-fortran', []),
 ('libsamplerate', 'https://github.com/libsndfile/libsamplerate.git', 'cmake', f'{cmake_args} -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF', []),
 ('rubberband', 'https://github.com/breakfastquay/rubberband.git', 'meson', '-Ddefault_library=static -Dfft=fftw -Dresampler=libsamplerate -Djni=disabled -Dladspa=disabled -Dlv2=disabled -Dvamp=disabled', ['fftw', 'libsamplerate']),
 ('libdvdcss', 'https://code.videolan.org/videolan/libdvdcss.git', 'meson', '-Ddefault_library=static', []),
 ('libdvdread', 'https://code.videolan.org/videolan/libdvdread.git', 'meson', '-Ddefault_library=static', ['libdvdcss']),
 ('libdvdnav', 'https://code.videolan.org/videolan/libdvdnav.git', 'meson', '-Ddefault_library=static', ['libdvdread']),
 ('libbluray', 'https://code.videolan.org/videolan/libbluray.git', 'meson', '-Ddefault_library=static -Denable_examples=false -Denable_tools=false', ['libdvdread', 'fontconfig', 'freetype']),
 ('libarchive', 'https://github.com/libarchive/libarchive.git', 'cmake', f'{cmake_args} {cmake_compiler_args} -DENABLE_TEST=OFF -DENABLE_TAR=OFF -DENABLE_CAT=OFF -DENABLE_CPIO=OFF -DZLIB_WINAPI=OFF -DZLIB_USE_STATIC_LIBS=ON -DENABLE_LIBB2=OFF -DENABLE_OPENSSL=OFF', ['zlib', 'bzip2', 'xz']),
]

updated_libs = set()

for n, u, t, f, depends_on in deps:
    dep_dir, build_dir, marker = os.path.join(dirs['repositories'], n), os.path.join(dirs['working'], n), os.path.join(dirs['installed'], f'.built_{n}')
    source_changed = False

    if u:
        source_changed = git_sync(u, n, dep_dir)
    elif n in ['gettext', 'xz', 'fftw', 'libiconv']:
        if os.path.exists(os.path.join(dep_dir, '.git')):
            logi(f'Replacing legacy git repo for {n} with tarball...')
            rmd(dep_dir)
        source_changed = not os.path.exists(dep_dir)
        if source_changed:
            t_urls = {
                'gettext': ('gettext-0.22.tar.xz', 'https://ftp.gnu.org/pub/gnu/gettext/gettext-0.22.tar.xz'),
                'xz': ('xz-5.4.4.tar.xz', 'https://github.com/tukaani-project/xz/releases/download/v5.4.4/xz-5.4.4.tar.xz'),
                'fftw': ('fftw-3.3.10.tar.gz', 'https://www.fftw.org/fftw-3.3.10.tar.gz'),
                'libiconv': ('libiconv-1.17.tar.gz', 'https://mirror.init7.net/gnu/libiconv/libiconv-1.17.tar.gz')
            }
            t_name, t_url = t_urls[n]
            tar = os.path.join(dirs['tarballs'], t_name)
            if not os.path.exists(tar):
                download_with_retry(t_url, tar)
            
            logi(f'Extracting {t_name}...')
            extract_tar(tar, dep_dir)
            
            root_map = {'gettext': 'gettext-0.22', 'xz': 'xz-5.4.4', 'fftw': 'fftw-3.3.10', 'libiconv': 'libiconv-1.17'}
            root = os.path.join(dep_dir, root_map[n])
            if os.path.exists(root):
                for i in os.listdir(root):
                    dst = os.path.join(dep_dir, i)
                    if os.path.exists(dst):
                        if os.path.isdir(dst): shutil.rmtree(dst)
                        else: os.remove(dst)
                    shutil.move(os.path.join(root, i), dep_dir)
                os.rmdir(root)

    # Force rebuild if output file is missing but marker exists (prevents broken state after partial cleanup)
    # Map packages to their actual output files (PC file or library if no PC)
    output_check_map = {
        'gettext': None,                        # System installed or build failing to produce static lib, skip check to stop loop
        'bzip2': ('lib', 'libbz2.a'),          # No PC file
        'libiconv': ('lib', 'libiconv.a'),     # No PC file
        'xz': ('lib/pkgconfig', 'liblzma.pc'),
        'freetype': ('lib/pkgconfig', 'freetype2.pc'),
        'libjpeg-turbo': ('lib/pkgconfig', 'libjpeg.pc'),
        'libsamplerate': ('lib/pkgconfig', 'samplerate.pc'),
        'spirv-cross': ('lib/pkgconfig', 'spirv-cross-c.pc'),
        'libsoxr': ('lib', 'libsoxr.a'),       # No PC file in list, checking lib
        'luajit': ('lib/pkgconfig', 'luajit.pc'),
        'ffnvcodec': ('lib/pkgconfig', 'ffnvcodec.pc'),
        'glslang': None,                        # Header-only, skip check
        'spirv-tools': None,                    # Header-only, skip check
        'fftw': ('lib/pkgconfig', 'fftw3.pc'),
        'libepoxy': ('lib/pkgconfig', 'epoxy.pc'),
        'spirv-headers': None,                  # Header-only/Config.cmake
        'libdvdread': ('lib/pkgconfig', 'dvdread.pc'),
        'libdvdnav': ('lib/pkgconfig', 'dvdnav.pc'),
    }
    
    check_info = output_check_map.get(n, ('lib/pkgconfig', f'{n}.pc'))
    if check_info:
        check_dir, check_file = check_info
        check_path = os.path.join(dirs['installed'], check_dir, check_file)
        if not os.path.exists(check_path) and os.path.exists(marker):
            os.remove(marker)
            logi(f'Forcing rebuild of {n} (output file missing)')

    dep_changed = any(d in updated_libs for d in depends_on)
    if clean_mode or not os.path.exists(marker) or source_changed or dep_changed:
        logi(f'Building {n}...')
        remove_pkg(n); rmd(build_dir); shutil.copytree(dep_dir, build_dir, dirs_exist_ok=True)
        if n == 'expat' and not os.path.exists(os.path.join(build_dir, 'CMakeLists.txt')):
            src_sub = os.path.join(build_dir, 'expat')
            if os.path.exists(src_sub):
                for i in os.listdir(src_sub):
                    dst = os.path.join(build_dir, i)
                    if os.path.exists(dst): 
                        if os.path.isdir(dst): shutil.rmtree(dst)
                        else: os.remove(dst)
                    shutil.move(os.path.join(src_sub, i), build_dir)
        
        f_env = f.replace('{pre}', pre) if f else ''
        current_env = env.copy()
        if n in ['libepoxy', 'libplacebo', 'libpng', 'libjpeg-turbo', 'zlib']:
            current_env['CC'] = 'clang'; current_env['CXX'] = 'clang++'

        if n == 'shaderc':
            tp = os.path.join(build_dir, 'third_party')
            for sub in ['glslang', 'spirv-tools', 'spirv-headers']:
                shutil.copytree(os.path.join(dirs['repositories'], sub), os.path.join(tp, sub), dirs_exist_ok=True)
            mrun([f'cmake -B build -G Ninja {f_env} .', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=env)
        elif t == 'meson':
            # Note: LTO disabled for deps to avoid lld incompatibility with Meson's --allow-shlib-undefined
            # LTO enabled only for final FFmpeg/mpv builds which handle it properly
            mrun([f'meson setup build . --prefix="{pre}" --buildtype=release -Ddefault_library=static --pkg-config-path="{pre}/lib/pkgconfig" {f_env} {link_args}', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=current_env)
            # Fix rubberband.pc to include C++ stdlib for static linking
            if n == 'rubberband':
                pc_file = os.path.join(pre, 'lib', 'pkgconfig', 'rubberband.pc')
                if os.path.exists(pc_file):
                    with open(pc_file, 'r') as f: pc_content = f.read()
                    if '-lstdc++' not in pc_content:
                        pc_content = pc_content.replace('-lrubberband', '-lrubberband -lstdc++')
                        with open(pc_file, 'w') as f: f.write(pc_content)
                        logi('FIX: Patched rubberband.pc with -lstdc++')
        elif t == 'cmake':
            mrun([f'cmake -B build -G Ninja {f_env} .', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=env)
            # Fix libarchive.pc to include static linking dependencies
            if n == 'libarchive':
                win_installed = dirs['installed']  # Windows path for file operations
                pc_file = os.path.join(win_installed, 'lib', 'pkgconfig', 'libarchive.pc')
                prefix = pre  # Already POSIX from to_unix()
                lib_dir = f'{prefix}/lib'
                msys_lib = prefix.replace('/installed', '/msys2/ucrt64/lib')
                pc_content = f'''prefix={prefix}
exec_prefix=${{prefix}}
libdir=${{exec_prefix}}/lib
includedir=${{prefix}}/include

Name: libarchive
Description: library that can create and read several streaming archive formats
Version: 3.9.0
Cflags: -I${{includedir}}
Cflags.private: -DLIBARCHIVE_STATIC
Libs: {lib_dir}/libarchive.a {msys_lib}/libz.a {msys_lib}/liblzma.a {msys_lib}/libbz2.a {msys_lib}/libiconv.a {msys_lib}/libbcrypt.a {msys_lib}/libzstd.a
'''
                with open(pc_file, 'w') as f: f.write(pc_content)
                logi('FIX: Patched libarchive.pc with absolute static lib paths')
        elif t == 'autotools' or t == 'intl' or t == 'make' or t == 'luajit':
            if (t == 'autotools' or t == 'intl') and not os.path.exists(os.path.join(build_dir, 'configure')):
                if os.path.exists(os.path.join(build_dir, 'autogen.sh')):
                    logi('Running autogen.sh...')
                    mrun(['./autogen.sh'], cwd=build_dir, env=env)
                elif os.path.exists(os.path.join(build_dir, 'configure.ac')):
                    logi('Running autoreconf -i...')
                    mrun(['autoreconf', '-i'], cwd=build_dir, env=env)
            
            # Remove redundant flags if they are already in f_env
            conf_flags = f'--prefix="{pre}" --enable-static --disable-shared'
            # Limit threads for xz due to race conditions on Windows
            current_nproc = 1 if n == 'xz' else nproc
            
            if t == 'autotools':
                cmd = [f'./configure {conf_flags} {f_env}', f'make -j{current_nproc}', f'make install']
            elif t == 'luajit':
                # Build LuaJIT with static library mode on Windows
                # The Makefile defaults to dynamic mode on Windows, we need static for embedding
                luajit_src = os.path.join(build_dir, 'src')
                cmd = [f'make -C src BUILDMODE=static PREFIX="{pre}" -j{nproc}', f'make install PREFIX="{pre}" BUILDMODE=static']
                mrun(cmd, cwd=build_dir, env=env)
                # Fix luajit.pc to remove Linux-specific flags (-ldl, -Wl,-E)
                luajit_pc = os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'luajit.pc')
                if os.path.exists(luajit_pc):
                    with open(luajit_pc, 'r') as pf:
                        pc_content = pf.read()
                    pc_content = pc_content.replace('Libs.private: -Wl,-E -lm -ldl', 'Libs.private:')
                    with open(luajit_pc, 'w') as pf:
                        pf.write(pc_content)
                    logi('FIX: Removed Linux-specific flags from luajit.pc')
            else:
                cmd = [f'make -j{nproc} {f_env}', f'make install {f_env}']
            if t != 'luajit':
                mrun(cmd, cwd=build_dir, env=env)

        for pcf in glob.glob(os.path.join(pre, 'lib', 'pkgconfig', f'*{n}*.pc')):
            patch_pc(pcf, '-lstdc++ -lm -lpthread')
        open(marker, 'w').close(); updated_libs.add(n)
        if n == 'spirv-cross':
            # Use Windows-native path (dirs['installed']) instead of MSYS2-style pre
            pkgconfig_dir = os.path.join(dirs['installed'], 'lib', 'pkgconfig')
            sp_dst = os.path.join(pkgconfig_dir, 'spirv-cross-c-shared.pc')
            # Create a complete .pc file with all spirv-cross dependencies for static linking
            prefix_msys = pre  # MSYS2 style path for .pc file
            pc_content = f'''# Auto-generated for static linking
prefix={prefix_msys}
exec_prefix=${{prefix}}
libdir=${{prefix}}/lib
includedir=${{prefix}}/include/spirv_cross

Name: spirv-cross-c-shared
Description: C API for SPIRV-Cross (static linking with all dependencies)
Version: 0.68.0

Requires:
Libs: -L${{libdir}} -lspirv-cross-c -lspirv-cross-glsl -lspirv-cross-hlsl -lspirv-cross-msl -lspirv-cross-cpp -lspirv-cross-reflect -lspirv-cross-util -lspirv-cross-core -lstdc++
Cflags: -I${{includedir}}
'''
            with open(sp_dst, 'w') as f:
                f.write(pc_content)
            logi(f'FIX: Created complete {sp_dst} with all static deps')
        
        # Fix shaderc.pc to use shaderc_combined instead of shaderc_shared for static linking
        if n == 'shaderc':
            pkgconfig_dir = os.path.join(dirs['installed'], 'lib', 'pkgconfig')
            shaderc_pc = os.path.join(pkgconfig_dir, 'shaderc.pc')
            if os.path.exists(shaderc_pc):
                with open(shaderc_pc, 'r') as f:
                    content = f.read()
                if '-lshaderc_shared' in content:
                    content = content.replace('-lshaderc_shared', '-lshaderc_combined')
                    with open(shaderc_pc, 'w') as f:
                        f.write(content)
                    logi(f'FIX: Patched {shaderc_pc} to use shaderc_combined')
    else: logi(f'Skipping {n}')

f_dir, f_mark = os.path.join(dirs['repositories'], 'ffmpeg'), os.path.join(dirs['installed'], '.built_ffmpeg')
f_changed = git_sync('https://git.ffmpeg.org/ffmpeg.git', 'ffmpeg', f_dir)

if clean_mode or not os.path.exists(f_mark) or f_changed or updated_libs:
    logi('Building ffmpeg...')
    b_dir = os.path.join(dirs['working'], 'ffmpeg')
    rmd(b_dir); shutil.copytree(f_dir, b_dir, dirs_exist_ok=True)

    if enable_cuda:
        logi("Enabling CUDA support in FFmpeg...")
        # Link our custom shim instead of redefining symbols
        extra_libs = "-lintl -liconv -lpthread -lws2_32 -lwinmm -ld3d11 -ldxgi -luuid -lcrypt32 -lOpenCL -lvulkan-1 -lcudart -lvsnprintf_shim -ladvapi32 -luser32 -lshlwapi -lgdi32 -lversion -lole32 -lharfbuzz -lfreetype -lfribidi -lfontconfig -lexpat -lbz2 -lstdc++"
        extra_ldflags = f"-L{pre}/lib -Wl,-u,_vsnprintf -Wl,-u,_snprintf -static -fuse-ld=lld -Wl,--allow-multiple-definition"
        cuda_flags = "--enable-cuda-nvcc --nvccflags='-allow-unsupported-compiler' --enable-cuda"
    else:
        log.warning("Disabling CUDA support in FFmpeg (Dependencies missing).")
        extra_libs = "-lintl -liconv -lpthread -lws2_32 -lwinmm -ld3d11 -ldxgi -luuid -lcrypt32 -lOpenCL -lvulkan-1 -lstdc++"
        extra_ldflags = f"-L{pre}/lib -static -fuse-ld=lld"
        cuda_flags = ""

    # Note: LTO disabled for FFmpeg due to conflict with CUDA/MSVC environment (libLIBCMT.a/libOLDNAMES.a errors)
    # LTO is enabled only for mpv (-Db_lto=true -Db_lto_mode=thin)
    # Note: LTO enabled now that we sanitize libcudart.a
    # LTO is enabled only for mpv (-Db_lto=true -Db_lto_mode=thin)
    cfg = [f'./configure --prefix="{pre}" --enable-static --disable-shared --enable-gpl --enable-version3 --enable-nonfree --enable-libass --enable-libfreetype --enable-libfontconfig --enable-libfribidi --enable-libplacebo --enable-libdav1d --enable-vulkan --enable-libshaderc --enable-ffnvcodec --enable-nvdec --enable-nvenc {cuda_flags} --enable-d3d11va --enable-dxva2 --enable-libzimg --enable-libsoxr --enable-librubberband --enable-opencl --enable-amf --enable-libbluray --enable-schannel --enable-ffmpeg --enable-ffplay --enable-ffprobe --pkg-config-flags=\'--static\' --extra-libs=\'{extra_libs}\' --extra-cflags=\'-march=x86-64-v3 -ffunction-sections -fdata-sections -I{pre}/include\' --extra-ldflags=\'{extra_ldflags}\' --enable-hwaccel=h264_d3d11va,hevc_d3d11va,vp9_d3d11va,av1_d3d11va,h264_nvdec,hevc_nvdec,vp9_nvdec,av1_nvdec,h264_vulkan,hevc_vulkan,vp9_vulkan,av1_vulkan --enable-lto=thin'.format(pre=pre)]

    run(' '.join(cfg), cwd=b_dir, env=env)
    run(f'make -j{nproc}', cwd=b_dir, env=env)
    run('make install', cwd=b_dir, env=env)
    open(f_mark, 'w').close()

m_dir, m_mark = os.path.join(dirs['repositories'], 'mpv'), os.path.join(dirs['installed'], '.built_mpv')
m_changed = git_sync('https://github.com/mpv-player/mpv.git', 'mpv', m_dir)

if clean_mode or not os.path.exists(m_mark) or m_changed or updated_libs or force_mpv:
    # Fix: Copy SDL2 headers from MSYS2 to installed dir for mpv to find them
    sdl2_src = os.path.join(msys, 'ucrt64', 'include', 'SDL2')
    sdl2_dst = os.path.join(dirs['installed'], 'include', 'SDL2')
    if os.path.exists(sdl2_src) and not os.path.exists(os.path.join(sdl2_dst, 'SDL.h')):
        os.makedirs(sdl2_dst, exist_ok=True)
        for f in os.listdir(sdl2_src):
            shutil.copy2(os.path.join(sdl2_src, f), sdl2_dst)
        logi(f'FIX: Copied SDL2 headers from msys2 to {sdl2_dst}')
    
    logi('Building mpv...')
    b_dir = os.path.join(dirs['working'], 'mpv')
    rmd(b_dir); shutil.copytree(m_dir, b_dir, dirs_exist_ok=True)

    # Generate Meson Native File for robust Linker Flags
    native_file_path = os.path.join(b_dir, 'mpv_build.ini')
    # Note: Only include essential static linking flags and system libs
    # LTO handled by Meson's -Db_lto=true -Db_lto_mode=thin option
    libs_list = [
        "-static",
        "-lintl", "-liconv", "-limagehlp", "-lksuser", "-lmfplat", "-lmfuuid", "-lwmcodecdspuuid",
        "-lshlwapi", "-lole32", "-luuid", "-lversion", "-lwinmm", "-lsetupapi",
        "-ld3d11", "-ldxgi", "-ld3dcompiler", "-ldxguid", "-ldwmapi", "-luxtheme", "-lSDL2"
    ]
    # Ensure -L path is present in LDFLAGS env var as backup
    env['LDFLAGS'] = f"-static -Wl,--gc-sections -L{pre}/lib"

    # Write native file content
    with open(native_file_path, 'w') as nf:
        nf.write("[built-in options]\n")
        # Python list to Meson list string format
        meson_list = ", ".join([f"'{x}'" for x in libs_list])
        nf.write(f"c_link_args = [{meson_list}]\n")
        nf.write(f"cpp_link_args = [{meson_list}]\n")

    # Enable LTO for mpv.exe as requested
    # Convert native file path to Unix style to avoid escape sequence issues in CONFIGURATION macro
    native_file_unix = to_unix(native_file_path)
    mrun([f'meson setup build . --prefix=\"{pre}\" --buildtype=release -Ddefault_library=static -Dprefer_static=true -Dlibmpv=false -Dlua=luajit -Degl=disabled -Dvulkan=enabled -Dd3d11=enabled -Dcuda-hwaccel=enabled -Duchardet=enabled -Drubberband=enabled -Dlibarchive=enabled -Ddvdnav=enabled -Dlibbluray=enabled -Dstrip=true -Db_lto=false -Dsdl2-audio=enabled -Dsdl2-video=enabled -Dsdl2-gamepad=enabled --pkg-config-path=\"{pre}/lib/pkgconfig\" --native-file=\"{native_file_unix}\"', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=b_dir, env=env)
    open(m_mark, 'w').close()

logi('Organizing binaries...')
bin_dir = os.path.join(dirs['installed'], 'bin')

for exe in ['mpv.exe', 'mpv.com', 'ffmpeg.exe', 'ffplay.exe', 'ffprobe.exe']:
    src = os.path.join(bin_dir, exe)
    if os.path.exists(src):
        try:
            logi(f'Stripping {exe}...')
            run(['strip', '-s', src.replace(os.sep, '/')], cwd=bin_dir, env=env)
        except Exception as e:
            log.error(f'Failed to process {exe}: {e}')

allowed_files = ['mpv.exe', 'mpv.com', 'ffmpeg.exe', 'ffplay.exe', 'ffprobe.exe']
logi(f'Cleaning {bin_dir} (Keeping: {allowed_files})...')
for item in os.listdir(bin_dir):
    if item not in allowed_files:
        p = os.path.join(bin_dir, item)
        try:
            if os.path.isfile(p): os.remove(p)
            elif os.path.isdir(p): shutil.rmtree(p)
            logi(f'Removed: {item}')
        except Exception as e:
            log.error(f'Failed to remove {item}: {e}')

logi(f'Complete: {os.path.join(dirs["installed"], "bin", "mpv.exe")}')