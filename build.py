# REVISION: v3.183
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
logi('# REVISION: v3.183')

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
logi('MPV Standalone Build v3.183 - Fixes: Enforce libdir=lib for Meson')

def rmd(d):
    if os.path.exists(d):
        e = tempfile.mkdtemp()
        subprocess.run(['robocopy', e, d, '/PURGE', '/E', '/R:0', '/W:0', '/NFL', '/NDL', '/NJH', '/NJS', '/NP'], capture_output=True, check=False)
        shutil.rmtree(d, ignore_errors=True); shutil.rmtree(e, ignore_errors=True)

def run(c, cwd=None, env=None):
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
    if not os.path.exists(d):
        logi(f'{n} missing. Cloning...')
        run(['git', 'clone', '--quiet', '--depth', '1', u, n] if n in ['ffmpeg', 'mpv'] else ['git', 'clone', '--quiet', u, n], cwd=os.path.dirname(d), env=env)
    else:
        try:
            current_url = run(['git', 'remote', 'get-url', 'origin'], cwd=d, env=env).stdout.strip()
            if current_url != u and not skip_updates:
                run(['git', 'remote', 'set-url', 'origin', u], cwd=d, env=env)
        except: pass

    if skip_updates:
        logi(f'Skipping update for {n} (User requested)')
        return False

    try:
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
        with open(path, 'r') as f: c = f.read()
        if libs not in c:
            if 'Libs.private:' in c: c = c.replace('Libs.private:', f'Libs.private: {libs}')
            elif 'Libs:' in c: c = c.replace('Libs:', f'Libs: {libs}')
            else: c += f'\nLibs: {libs}'

        if '-ldl' in c: c = c.replace('-ldl', '')

        with open(path, 'w') as f: f.write(c)
        logi(f'Patched {os.path.basename(path)}')
    except Exception as e: log.error(f'PC patch error: {e}')

def create_pc(name, ver, desc, libs, reqs='', cflags='-I${includedir}'):
    pc_path = os.path.join(dirs['installed'], 'lib', 'pkgconfig', f'{name}.pc')
    lib64_pc = os.path.join(dirs['installed'], 'lib64', 'pkgconfig', f'{name}.pc')
    if os.path.exists(lib64_pc): os.remove(lib64_pc)

    logi(f'Creating/Overwriting {name}.pc...')
    c = f'prefix={pre}\nexec_prefix=${{prefix}}\nlibdir=${{exec_prefix}}/lib\nincludedir=${{prefix}}/include\n\nName: {name}\nDescription: {desc}\nVersion: {ver}\nRequires.private: {reqs}\nLibs: -L${{libdir}} {libs}\nCflags: {cflags}\n'
    with open(pc_path, 'w') as f: f.write(c)

def clean_libs():
    logi('Sweeping lib folder for import libraries...')
    for lib_dir in ['lib', 'lib64']:
        p = os.path.join(dirs['installed'], lib_dir)
        if not os.path.exists(p): continue
        for ext in ['*.dll.a', '*.dll', '*.la']:
            for f in glob.glob(os.path.join(p, ext)):
                try: os.remove(f); logi(f'Purged {os.path.basename(f)}')
                except: pass

    for bad in ['libz.dll.a', 'libstdc++.dll.a', 'libgcc_s.dll.a', 'libz.a', 'libzlibstatic.a']:
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
        'fftw': 'fftw3', 'libsamplerate': 'samplerate', 'rubberband': 'rubberband'
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
        if n == 'spirv-cross':
             for f in glob.glob(os.path.join(pk, 'spirv-cross-c-shared.pc')):
                 try: os.remove(f); logi(f'Cleaned alias {os.path.basename(f)}')
                 except: pass

def dump_meson_summary(build_dir):
    log_path = os.path.join(build_dir, 'meson-logs', 'meson-log.txt')
    if os.path.exists(log_path):
        logi('\n--- MESON BUILD SUMMARY ---')
        try:
            with open(log_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()
                if "Build targets in project" in content:
                    idx = content.rfind("Build targets in project")
                    chunk = content[max(0, idx-3000):idx]
                    logi(chunk)
                else:
                    for feature in ['opengl', 'd3d11', 'vulkan', 'shaderc', 'lcms', 'glslang', 'cuda-hwaccel', 'cuda-interop']:
                        found = False
                        if f"Run-time dependency {feature} found: YES" in content: found = True
                        if f"Library {feature} found: YES" in content: found = True
                        if f"Option {feature} found: YES" in content: found = True
                        status = "ENABLED" if found else "NOT FOUND/DISABLED"
                        logi(f"  {feature.upper()}: {status}")
        except: pass
        logi('---------------------------\n')

def debug_pkg_config():
    logi('\n--- AVAILABLE PACKAGES (PKG-CONFIG) ---')
    pc_dir = os.path.join(dirs['installed'], 'lib', 'pkgconfig')
    if os.path.exists(pc_dir):
        pcs = [os.path.basename(f).replace('.pc', '') for f in glob.glob(os.path.join(pc_dir, '*.pc'))]
        logi(f"Found {len(pcs)} packages: {', '.join(sorted(pcs))}")
    else:
        log.error(f"PKG-CONFIG DIR MISSING: {pc_dir}")

    logi("DEBUG: Resolving zlib libs...")
    try:
        out = run([env['PKG_CONFIG'], '--libs', 'zlib'], env=env).stdout.strip()
        logi(f"PKG-CONFIG ZLIB: {out}")
    except: pass
    logi('---------------------------------------')

base = os.path.dirname(os.path.abspath(__file__))
bd = os.path.join(base, 'mpv-standalone-build')
dirs = {k: os.path.join(bd, k) for k in ['repositories', 'tarballs', 'installed', 'working', '.ccache', 'msys2']}
for d in dirs.values(): os.makedirs(d, exist_ok=True)

to_unix = lambda p: p.replace('\\', '/').replace('C:/', '/c/')
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
            url = 'https://repo.msys2.org/distrib/x86_64/msys2-x86_64-20241108.tar.xz'
        urllib.request.urlretrieve(url, tar)
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
env.update({'MSYSTEM': 'UCRT64', 'CHERE_INVOKING': '1', 'LC_ALL': 'C', 'CC': 'ccache gcc', 'CXX': 'ccache g++',
            'CFLAGS': f'-static -O3 -march=x86-64-v3 -fPIC -static-libgcc -static-libstdc++ -ffunction-sections -fdata-sections -I{pre}/include',
            'CXXFLAGS': f'-static -O3 -march=x86-64-v3 -fPIC -static-libgcc -static-libstdc++ -ffunction-sections -fdata-sections -I{pre}/include',
            'LDFLAGS': f'-static -static-libgcc -static-libstdc++ -Wl,--gc-sections -L{pre}/lib',
            'CCACHE_DIR': dirs['.ccache'],
            'PKG_CONFIG': 'pkg-config --static', 'PKG_CONFIG_LIBDIR': pcpath, 'PKG_CONFIG_PATH': pcpath})
env['PATH'] = os.pathsep.join([os.path.join(dirs['installed'], 'bin'), os.path.join(msys, 'ucrt64', 'bin'), os.path.join(msys, 'usr', 'bin'), env['PATH']])

if not skip_updates: mrun(['pacman -Syu --noconfirm', 'pacman -Syu --noconfirm'], env=env)

logi('Installing tools')
mrun(['pacman -S --noconfirm --needed mingw-w64-ucrt-x86_64-toolchain mingw-w64-ucrt-x86_64-ccache mingw-w64-ucrt-x86_64-cmake mingw-w64-ucrt-x86_64-meson mingw-w64-ucrt-x86_64-ninja git mingw-w64-ucrt-x86_64-yasm mingw-w64-ucrt-x86_64-nasm mingw-w64-ucrt-x86_64-autotools mingw-w64-ucrt-x86_64-gettext patch mingw-w64-ucrt-x86_64-gperf groff mingw-w64-ucrt-x86_64-python mingw-w64-ucrt-x86_64-python-pip autoconf automake libtool mingw-w64-ucrt-x86_64-vulkan-headers mingw-w64-ucrt-x86_64-vulkan-loader'], env=env)

logi('Installing python deps')
mrun(['python -m pip install Jinja2'], env=env)

# Robust copying of system PC files
sys_pkg = os.path.join(msys, 'ucrt64', 'lib', 'pkgconfig')
loc_pkg = os.path.join(dirs['installed'], 'lib', 'pkgconfig')
os.makedirs(loc_pkg, exist_ok=True)
for pattern in ['vulkan.pc']:
    for f in glob.glob(os.path.join(sys_pkg, pattern)):
        shutil.copy(f, loc_pkg)
        logi(f'Imported {os.path.basename(f)}')
        patch_pc(os.path.join(loc_pkg, os.path.basename(f)), '-lstdc++ -lm -lpthread')

for f in glob.glob(os.path.join(loc_pkg, '*shared*')):
    os.remove(f)

# Import the actual static libraries so the linker can find them
sys_lib = os.path.join(msys, 'ucrt64', 'lib')
loc_lib = os.path.join(dirs['installed'], 'lib')
os.makedirs(loc_lib, exist_ok=True)
for pattern in ['libvulkan*.dll.a']:
    for f in glob.glob(os.path.join(sys_lib, pattern)):
        shutil.copy(f, loc_lib)
        logi(f'Imported library {os.path.basename(f)}')

# Fix: Create static aliases
src_vlk = os.path.join(loc_lib, 'libvulkan-1.dll.a')
dst_vlk = os.path.join(loc_lib, 'libvulkan-1.a')
if os.path.exists(src_vlk): shutil.copy(src_vlk, dst_vlk); logi('Created alias libvulkan-1.a')

link_args = "-Dc_link_args='-static -static-libgcc -static-libstdc++ -lintl -liconv' -Dcpp_link_args='-static -static-libgcc -static-libstdc++ -lintl -liconv' "
cmake_args = f'-DBUILD_SHARED_LIBS=OFF -DCMAKE_INSTALL_PREFIX="{pre}" -DCMAKE_PREFIX_PATH="{pre}" -DCMAKE_INSTALL_LIBDIR=lib'
cmake_compiler_args = '-DCMAKE_C_COMPILER=gcc -DCMAKE_CXX_COMPILER=g++'
zlib_cmake_flags = f'-DZLIB_LIBRARY="{static_zlib}" -DZLIB_INCLUDE_DIR="{pre}/include"'

deps = [
 ('expat','https://github.com/libexpat/libexpat.git','cmake', f'{cmake_args} -DEXPAT_BUILD_DOCS=OFF -DEXPAT_BUILD_EXAMPLES=OFF -DEXPAT_BUILD_TESTS=OFF -DEXPAT_BUILD_TOOLS=OFF'),
 ('gettext',None,'intl',''),
 ('bzip2','https://sourceware.org/git/bzip2.git','make',''),
 ('zlib', 'https://github.com/madler/zlib.git', 'cmake', f'{cmake_args} {cmake_compiler_args}'),
 ('xz', None, 'autotools','--enable-static --disable-shared --disable-nls'),
 ('zimg', 'https://github.com/sekrit-twc/zimg.git', 'autotools', '--enable-static --disable-shared'),
 ('libpng', 'https://github.com/glennrp/libpng.git', 'cmake', f'{cmake_args} {cmake_compiler_args} -DPNG_SHARED=OFF -DPNG_TESTS=OFF -DPNG_EXECUTABLES=OFF {zlib_cmake_flags}'),
 ('libjpeg-turbo', 'https://github.com/libjpeg-turbo/libjpeg-turbo.git', 'cmake', f'{cmake_args} {cmake_compiler_args} -DWITH_TURBOJPEG=OFF -DENABLE_SHARED=OFF -DENABLE_STATIC=ON {zlib_cmake_flags}'),
 ('freetype','https://gitlab.freedesktop.org/freetype/freetype.git','meson','-Dpng=disabled -Dbzip2=disabled -Dbrotli=disabled -Dzlib=disabled -Dharfbuzz=disabled -Dtests=disabled'),
 ('libiconv','https://git.savannah.gnu.org/git/libiconv.git','autotools','--enable-static --disable-shared'),
 ('fribidi','https://github.com/fribidi/fribidi.git','meson','-Ddocs=false -Dtests=false -Ddefault_library=static'),
 ('harfbuzz','https://github.com/harfbuzz/harfbuzz.git','cmake','-DBUILD_SHARED_LIBS=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="{pre}" -DHB_HAVE_FREETYPE=ON -DHB_HAVE_ICU=OFF -DHB_HAVE_GLIB=OFF -DHB_HAVE_GOBJECT=OFF -DHB_HAVE_GRAPHITE2=OFF -DHB_BUILD_TESTS=OFF -DHB_BUILD_UTILS=OFF -DCMAKE_PREFIX_PATH="{pre}" -DCMAKE_INSTALL_LIBDIR=lib'),
 ('fontconfig','https://gitlab.freedesktop.org/fontconfig/fontconfig.git','meson','-Dtests=disabled -Ddoc=disabled'),
 ('libass','https://github.com/libass/libass.git','autotools','--enable-static --disable-shared --enable-fontconfig'),
 ('lcms2', 'https://github.com/mm2/Little-CMS.git', 'meson', '-Ddefault_library=static'),
 ('libepoxy', 'https://github.com/anholt/libepoxy.git', 'meson', '-Dtests=false -Dglx=no -Degl=no'),
 ('spirv-headers', 'https://github.com/KhronosGroup/SPIRV-Headers.git', 'cmake', f'{cmake_args} -DSPIRV_HEADERS_SKIP_EXAMPLES=ON -DSPIRV_HEADERS_SKIP_INSTALL_EXAMPLES=ON'),
 ('spirv-cross', 'https://github.com/KhronosGroup/SPIRV-Cross.git', 'cmake', f'{cmake_args} -DSPIRV_CROSS_CLI=OFF -DSPIRV_CROSS_ENABLE_TESTS=OFF'),
 ('glslang', 'https://github.com/KhronosGroup/glslang.git', None, None),
 ('spirv-tools', 'https://github.com/KhronosGroup/SPIRV-Tools.git', None, None),
 ('shaderc', 'https://github.com/google/shaderc.git', 'cmake', f'{cmake_args} -DSHADERC_SKIP_TESTS=ON -DSHADERC_SKIP_EXAMPLES=ON -DSHADERC_SKIP_COPYRIGHT_CHECK=ON -DSHADERC_ENABLE_SHARED_CRT=OFF'),
 ('ffnvcodec', 'https://git.videolan.org/git/ffmpeg/nv-codec-headers.git', 'make', 'PREFIX="{pre}"'),
 # FIX: Enforce libdir=lib for dav1d to prevent infinite rebuilds on 64-bit systems
 ('dav1d', 'https://code.videolan.org/videolan/dav1d.git', 'meson', '-Denable_tests=false -Denable_tools=false --libdir=lib'),
 ('libplacebo','https://code.videolan.org/videolan/libplacebo.git','meson','-Dopengl=enabled -Dd3d11=enabled -Dvulkan=enabled -Dshaderc=enabled -Dlcms=enabled -Dtests=false -Ddemos=false -Dxxhash=disabled -Dlibdovi=disabled'),
 ('luajit','https://github.com/LuaJIT/LuaJIT.git','luajit',''),
 ('uchardet', 'https://gitlab.freedesktop.org/uchardet/uchardet.git', 'cmake', f'{cmake_args} -DBUILD_BINARY=OFF -DBUILD_SHARED_LIBS=OFF'),
 ('libsoxr', 'https://git.code.sf.net/p/soxr/code', 'cmake', f'{cmake_args} -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTS=OFF -DWITH_OPENMP=OFF'),
 ('fftw', None, 'autotools', '--enable-static --disable-shared --disable-doc --enable-threads --enable-sse2 --enable-avx2 --with-our-malloc --with-combined-threads'),
 ('libsamplerate', 'https://github.com/libsndfile/libsamplerate.git', 'cmake', f'{cmake_args} -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DBUILD_SHARED_LIBS=OFF -DBUILD_TESTING=OFF'),
 ('rubberband', 'https://github.com/breakfastquay/rubberband.git', 'meson', '-Ddefault_library=static -Dfft=fftw -Dresampler=libsamplerate -Djni=disabled -Dladspa=disabled -Dlv2=disabled -Dvamp=disabled'),
]

deps_rebuilt = False
for n, u, t, f in deps:
    dep_dir, build_dir, marker = os.path.join(dirs['repositories'], n), os.path.join(dirs['working'], n), os.path.join(dirs['installed'], f'.built_{n}')
    changed = False

    if u:
        changed = git_sync(u, n, dep_dir)
    elif n == 'gettext':
        tar = os.path.join(dirs['tarballs'], 'gettext-0.22.tar.xz')
        if not os.path.exists(tar):
            for url in ['https://ftp.gnu.org/pub/gnu/gettext/gettext-0.22.tar.xz', 'https://ftpmirror.gnu.org/gettext/gettext-0.22.tar.xz', 'https://mirror.selfnet.de/gnu/gettext/gettext-0.22.tar.xz']:
                try: logi(f'Downloading gettext...'); urllib.request.urlretrieve(url, tar); break
                except: pass
            if not os.path.exists(tar): log.error('gettext download failed.'); sys.exit(1)
        if not os.path.exists(dep_dir):
            os.makedirs(dep_dir, exist_ok=True)
            extract_tar(tar, dep_dir)
            root = os.path.join(dep_dir, 'gettext-0.22')
            if os.path.exists(root):
                for i in os.listdir(root): shutil.move(os.path.join(root, i), dep_dir)
                os.rmdir(root)
            changed = True
    elif n == 'xz':
        tar = os.path.join(dirs['tarballs'], 'xz-5.6.3.tar.xz')
        if not os.path.exists(tar):
            try:
                logi(f'Downloading xz...'); urllib.request.urlretrieve('https://github.com/tukaani-project/xz/releases/download/v5.6.3/xz-5.6.3.tar.xz', tar)
            except: pass
            if not os.path.exists(tar): log.error('xz download failed.'); sys.exit(1)
        if os.path.exists(dep_dir) and not os.path.exists(os.path.join(dep_dir, 'configure')):
            rmd(dep_dir)
        if not os.path.exists(dep_dir):
            os.makedirs(dep_dir, exist_ok=True)
            extract_tar(tar, dep_dir)
            root = os.path.join(dep_dir, 'xz-5.6.3')
            if os.path.exists(root):
                for i in os.listdir(root): shutil.move(os.path.join(root, i), dep_dir)
                os.rmdir(root)
            changed = True
    elif n == 'fftw':
        tar = os.path.join(dirs['tarballs'], 'fftw-3.3.10.tar.gz')
        if not os.path.exists(tar):
            logi('Downloading FFTW...')
            try: urllib.request.urlretrieve('https://www.fftw.org/fftw-3.3.10.tar.gz', tar)
            except: pass
            if not os.path.exists(tar): log.error('FFTW download failed.'); sys.exit(1)
        if os.path.exists(dep_dir) and not os.path.exists(os.path.join(dep_dir, 'configure')):
            rmd(dep_dir)
        if not os.path.exists(dep_dir):
            os.makedirs(dep_dir, exist_ok=True)
            extract_tar(tar, dep_dir)
            root = os.path.join(dep_dir, 'fftw-3.3.10')
            if os.path.exists(root):
                for i in os.listdir(root): shutil.move(os.path.join(root, i), dep_dir)
                os.rmdir(root)
            changed = True

    if n == 'luajit' and not os.path.exists(os.path.join(dirs['installed'], 'lib', 'libluajit.a')):
        logi('LuaJIT static lib missing. Forcing rebuild.')
        changed = True

    if os.path.exists(marker):
        pc_map = {
            'gettext': 'libintl', 'xz': 'liblzma', 'libjpeg-turbo': 'libjpeg', 'freetype': 'freetype2',
            'spirv-cross': None,
            'spirv-headers': None,
            'spirv-tools': 'SPIRV-Tools', 'glslang': None,
            'libepoxy': 'epoxy', 'ffnvcodec': 'ffnvcodec', 'bzip2': None,
            'libsoxr': 'soxr', 'fftw': 'fftw3', 'libsamplerate': 'samplerate'
        }
        pc_name = pc_map.get(n, n)
        if pc_name:
             pc_path = os.path.join(dirs['installed'], 'lib', 'pkgconfig', f'{pc_name}.pc')
             if not os.path.exists(pc_path):
                 lib_name = f"lib{n}.a"
                 if n == 'freetype': lib_name = "libfreetype.a"
                 elif n == 'libsoxr': lib_name = "libsoxr.a"
                 elif n == 'fftw': lib_name = "libfftw3.a"

                 lib_path = os.path.join(dirs['installed'], 'lib', lib_name)
                 if not os.path.exists(lib_path):
                     log.warning(f'CORRUPTION: {n} is marked as built but {pc_name}.pc AND {lib_name} are missing. Forcing rebuild.')
                     changed = True
                 else:
                     logi(f"Warning: {pc_name}.pc missing for {n}, but {lib_name} exists. Skipping rebuild.")

        if n == 'spirv-cross' and not os.path.exists(os.path.join(dirs['installed'], 'lib', 'libspirv-cross-c.a')): changed = True
        if n == 'shaderc' and not os.path.exists(os.path.join(dirs['installed'], 'lib', 'libshaderc_combined.a')): changed = True
        if n == 'libplacebo' and not os.path.exists(os.path.join(dirs['installed'], 'lib', 'libplacebo.a')): changed = True
        if n == 'glslang' and not os.path.exists(os.path.join(dirs['installed'], 'lib', 'libglslang.a')): changed = True
        if n == 'spirv-headers' and not os.path.exists(os.path.join(dirs['installed'], 'include', 'spirv', 'unified1', 'spirv.h')): changed = True

    if deps_rebuilt: changed = True

    if clean_mode or not os.path.exists(marker) or changed:
        logi(f'Building {n}...')
        remove_pkg(n)

        rmd(build_dir); shutil.copytree(dep_dir, build_dir, dirs_exist_ok=True)
        f_env = f.replace('{pre}', pre) if f else ''

        current_env = env.copy()
        if n in ['libepoxy', 'libplacebo', 'libpng', 'libjpeg-turbo', 'zlib']:
            current_env['CC'] = 'gcc'; current_env['CXX'] = 'g++'

        if n == 'libepoxy':
             mrun([f'meson setup build . --prefix="{pre}" --buildtype=release -Ddefault_library=static --pkg-config-path="{pre}/lib/pkgconfig" {f_env} {link_args}', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=current_env)
        elif n == 'shaderc':
            tp = os.path.join(build_dir, 'third_party')
            os.makedirs(tp, exist_ok=True)
            for sub in ['glslang', 'spirv-tools', 'spirv-headers']:
                src = os.path.join(dirs['repositories'], sub); dst = os.path.join(tp, sub)
                if os.path.exists(src):
                    if os.path.exists(dst): rmd(dst)
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    logi(f'Injected {sub} into shaderc')
            mrun([f'cmake -B build -G Ninja {f_env} .', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=env)
        elif n == 'expat':
             mrun([f'cmake -B build -G Ninja {f_env} expat', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=env)
        elif n == 'gettext':
            mrun([f'cd gettext-runtime && ./configure --prefix="{pre}" --enable-static --disable-shared {f_env}', 'cd gettext-runtime && make -j1', 'cd gettext-runtime && make install'], cwd=build_dir, env=env)
            create_pc('libintl', '0.22', 'GNU gettext runtime library', '-lintl')
        elif n == 'xz':
            mrun([f'./configure --prefix="{pre}" --enable-static --disable-shared --disable-nls {f_env}', f'make -j{nproc}', 'make install'], cwd=build_dir, env=env)
        elif n == 'bzip2':
            mrun([f"make -j{nproc} CC='ccache gcc' CFLAGS='-O3 -march=x86-64-v3 -static -fPIC -D_FILE_OFFSET_BITS=64'", f'make install PREFIX="{pre}"'], cwd=build_dir, env=env)
        elif n == 'zimg':
             mrun(['./autogen.sh'], cwd=build_dir, env=env)
             mrun([f'./configure --prefix="{pre}" --enable-static --disable-shared {f_env}', f'make -j{nproc}', 'make install'], cwd=build_dir, env=env)
        elif n == 'zlib':
             clean_libs()
             mrun([f'cmake -B build -G Ninja {f_env} .', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=current_env)
             src_z = os.path.join(dirs['installed'], 'lib', 'libzlibstatic.a'); dst_z = os.path.join(dirs['installed'], 'lib', 'libz.a')
             if os.path.exists(src_z) and not os.path.exists(dst_z): shutil.copy(src_z, dst_z); logi('Aliased libzlibstatic.a to libz.a')
             create_pc('zlib', '1.3', 'zlib compression library', '-lz')
        elif n == 'libpng' or n == 'libjpeg-turbo':
             mrun([f'cmake -B build -G Ninja {f_env} .', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=current_env)
        elif n == 'ffnvcodec':
             mrun([f'make install PREFIX="{pre}"'], cwd=build_dir, env=env)
        elif n == 'fribidi':
            mb = os.path.join(build_dir, 'meson.build')
            if os.path.exists(mb):
                with open(mb, 'r') as f_in: c = f_in.read()
                with open(mb, 'w') as f_out: f_out.write(c.replace("subdir('bin')", "#subdir('bin')"))
            mrun([f'meson setup build . --prefix="{pre}" --buildtype=release -Ddefault_library=static --pkg-config-path="{pre}/lib/pkgconfig" {f_env} {link_args}', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=env)
        elif t == 'autotools':
            if n == 'libiconv': mrun(['./gitsub.sh pull', './autogen.sh'], cwd=build_dir, env=env)
            elif n in ['libass', 'fribidi']: mrun(['./autogen.sh'], cwd=build_dir, env=env)
            # FIX: Removed bootstrap for fftw (not needed for tarball)
            mrun([f'./configure --prefix="{pre}" --enable-static --disable-shared {f_env}', f'make -j{nproc}', 'make install'], cwd=build_dir, env=env)
            if n == 'libiconv': create_pc('libiconv', '1.17', 'GNU libiconv', '-liconv')
        elif t == 'meson':
            mrun([f'meson setup build . --prefix="{pre}" --buildtype=release -Ddefault_library=static --pkg-config-path="{pre}/lib/pkgconfig" {f_env} {link_args}', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=current_env)
            if n == 'libplacebo': dump_meson_summary(build_dir)
        elif t == 'cmake':
            mrun([f'cmake -B build -G Ninja {f_env} .', f'ninja -C build -j{nproc}', 'ninja -C build install'], cwd=build_dir, env=env)
        elif t == 'luajit':
            mrun([f"make -j{nproc} CC='ccache gcc' BUILDMODE=static"], cwd=build_dir, env=env)
            mrun([f'make install PREFIX="{pre}"'], cwd=build_dir, env=env)
            src_a = os.path.join(build_dir, 'src', 'libluajit.a'); dst_a = os.path.join(dirs['installed'], 'lib', 'libluajit.a')
            if os.path.exists(src_a): shutil.copy(src_a, dst_a)

        clean_libs()
        open(marker, 'w').close(); deps_rebuilt = True
    else: logi(f'Skipping {n}')

    if n == 'gettext': create_pc('libintl', '0.22', 'GNU gettext runtime library', '-lintl')
    elif n == 'libiconv': create_pc('libiconv', '1.17', 'GNU libiconv', '-liconv')
    elif n == 'fontconfig': patch_pc(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'fontconfig.pc'), '-lintl -liconv')
    elif n == 'freetype': patch_pc(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'freetype2.pc'), '-lbz2 -lpng -lharfbuzz -lintl -liconv')
    elif n == 'libass': patch_pc(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'libass.pc'), '-lharfbuzz -lintl -liconv')
    elif n == 'shaderc':
        create_pc('shaderc', '2023.7', 'shaderc', '-lshaderc_combined -lstdc++ -lm -lpthread')
        src = os.path.join(dirs['installed'], 'lib', 'libshaderc_combined.a')
        if os.path.exists(src):
            shutil.copy(src, os.path.join(dirs['installed'], 'lib', 'libshaderc_shared.a'))
            shutil.copy(src, os.path.join(dirs['installed'], 'lib', 'libshaderc.a'))
            logi('Created static aliases: libshaderc_shared.a, libshaderc.a')
    elif n == 'spirv-cross':
         libs_list = ['-lspirv-cross-c', '-lspirv-cross-glsl', '-lspirv-cross-hlsl', '-lspirv-cross-reflect', '-lspirv-cross-util', '-lspirv-cross-core', '-lstdc++']
         if os.path.exists(os.path.join(dirs['installed'], 'lib', 'libspirv-cross-msl.a')): libs_list.insert(3, '-lspirv-cross-msl')
         if os.path.exists(os.path.join(dirs['installed'], 'lib', 'libspirv-cross-cpp.a')): libs_list.insert(4, '-lspirv-cross-cpp')
         create_pc('spirv-cross-c-shared', '0.67.0', 'SPIRV-Cross C Shared', ' '.join(libs_list), cflags='-I${includedir}/spirv_cross -I${includedir}')
         src_a = os.path.join(dirs['installed'], 'lib', 'libspirv-cross-c.a'); dst_a = os.path.join(dirs['installed'], 'lib', 'libspirv-cross-c-shared.a')
         if os.path.exists(src_a): shutil.copy(src_a, dst_a)

    elif n == 'libplacebo':
        pc_file = os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'libplacebo.pc')
        patch_pc(pc_file, '-lstdc++ -lm -lgcc -lpthread -lvulkan-1 -lshlwapi -lgdi32 -lbcrypt -lole32 -luuid -luser32 -ld3d11 -ldxgi -lversion -llcms2 -lsetupapi -ladvapi32 -lepoxy')
        if os.path.exists(pc_file):
             with open(pc_file, 'r') as f: c = f.read()
             if 'shaderc_shared' in c:
                 c = c.replace('shaderc_shared', 'shaderc_combined')
                 with open(pc_file, 'w') as f: f.write(c)
    elif n == 'harfbuzz': patch_pc(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'harfbuzz.pc'), '-lstdc++')
    elif n == 'dav1d': patch_pc(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'dav1d.pc'), '-lpthread')
    elif n == 'luajit':
        patch_pc(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'luajit.pc'), '-lm')
        src_pc = os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'luajit.pc')
        if os.path.exists(src_pc):
            lib_src = os.path.join(dirs['installed'], 'lib', 'libluajit.a'); lib_dst = os.path.join(dirs['installed'], 'lib', 'libluajit-5.1.a')
            if os.path.exists(lib_src): shutil.copy(lib_src, lib_dst)
            for alias in ['luajit-5.1.pc', 'lua51.pc', 'lua.pc']:
                dst = os.path.join(dirs['installed'], 'lib', 'pkgconfig', alias)
                if not os.path.exists(dst): shutil.copy(src_pc, dst)

    elif n == 'rubberband': patch_pc(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'rubberband.pc'), '-lstdc++')

f_dir, f_mark = os.path.join(dirs['repositories'], 'ffmpeg'), os.path.join(dirs['installed'], '.built_ffmpeg')
f_changed = git_sync('https://git.ffmpeg.org/ffmpeg.git', 'ffmpeg', f_dir)

debug_pkg_config()
check_pass = True
check_pass &= (os.path.exists(os.path.join(dirs['installed'], 'lib', 'pkgconfig', 'libass.pc')))
spirv_h = os.path.join(dirs['installed'], 'include', 'spirv_cross', 'spirv_cross_c.h')
if not os.path.exists(spirv_h):
    log.error(f"CRITICAL: spirv_cross_c.h not found at {spirv_h}. Build is likely to fail.")
    check_pass = False

if not check_pass:
    log.error("Integrity checks failed. Aborting.")
    sys.exit(1)

clean_libs()
src_z = os.path.join(dirs['installed'], 'lib', 'libzlibstatic.a'); dst_z = os.path.join(dirs['installed'], 'lib', 'libz.a')
if os.path.exists(src_z): shutil.copy(src_z, dst_z); logi('Restored libz.a alias for FFmpeg')


if clean_mode or not os.path.exists(f_mark) or f_changed or deps_rebuilt:
    logi('Building ffmpeg...')
    remove_pkg('ffmpeg')
    for lib in ['avcodec', 'avdevice', 'avfilter', 'avformat', 'avutil', 'swresample', 'swscale', 'postproc']:
        remove_pkg(lib)

    b_dir = os.path.join(dirs['working'], 'ffmpeg')
    rmd(b_dir); shutil.copytree(f_dir, b_dir, dirs_exist_ok=True)

    cfg = [f'./configure --prefix="{pre}" --enable-static --disable-shared --enable-gpl --enable-version3 --enable-nonfree --enable-libass --enable-libfreetype --enable-libfontconfig --enable-libfribidi --enable-libplacebo --enable-libdav1d --enable-vulkan --enable-libshaderc --enable-ffnvcodec --enable-nvdec --enable-d3d11va --enable-dxva2 --disable-doc --disable-programs --enable-libzimg --enable-libsoxr --enable-librubberband --enable-lto --pkg-config-flags=\'--static\' --extra-libs=\'-Wl,-Bstatic -static-libgcc -static-libstdc++ -lintl -liconv -lstdc++ -lpthread -lws2_32 -lwinmm -ld3d11 -ldxgi -luuid -lcrypt32\' --extra-cflags=\'-march=x86-64-v3 -ffunction-sections -fdata-sections\' --enable-hwaccel=h264_d3d11va,hevc_d3d11va,vp9_d3d11va,av1_d3d11va,h264_nvdec,hevc_nvdec,vp9_nvdec,av1_nvdec,h264_vulkan,hevc_vulkan,vp9_vulkan,av1_vulkan']

    conf_res = run(' '.join(cfg), cwd=b_dir, env=env)
    if conf_res and conf_res.stdout:
        logi('--- FFMPEG CONFIG SUMMARY (Partial) ---')
        for line in conf_res.stdout.splitlines()[-50:]:
            if any(k in line for k in ['External libraries:', 'Hardware accelerators:']):
                 logi(line)

    run(f'make -j{nproc}', cwd=b_dir, env=env)
    run('make install', cwd=b_dir, env=env)
    open(f_mark, 'w').close()
else: logi('Skipping ffmpeg')

m_dir, m_mark = os.path.join(dirs['repositories'], 'mpv'), os.path.join(dirs['installed'], '.built_mpv')
m_changed = git_sync('https://github.com/mpv-player/mpv.git', 'mpv', m_dir)

if clean_mode or not os.path.exists(m_mark) or m_changed or deps_rebuilt or f_changed or force_mpv:
    logi('Building mpv...')
    b_dir = os.path.join(dirs['working'], 'mpv')
    rmd(b_dir); shutil.copytree(m_dir, b_dir, dirs_exist_ok=True)

    clean_libs()
    link_args_static = "-static -static-libgcc -static-libstdc++"
    mpv_meson_args = f"-Dc_link_args='{link_args_static}' -Dcpp_link_args='{link_args_static}'"

    mrun([f'meson setup build . --prefix="{pre}" --buildtype=release -Ddefault_library=static -Dprefer_static=true -Dlibmpv=false -Dlua=luajit -Degl=disabled -Dvulkan=enabled -Dd3d11=enabled -Dcuda-hwaccel=enabled -Duchardet=enabled -Drubberband=enabled -Dlibarchive=disabled -Dstrip=true -Db_lto=true --pkg-config-path="{pre}/lib/pkgconfig" {mpv_meson_args}', f'ninja -C build -v -j{nproc}', 'ninja -C build install'], cwd=b_dir, env=env)
    dump_meson_summary(b_dir)

    with open(m_mark, 'w') as f: f.write('')
    for f in os.listdir(os.path.join(dirs['installed'], 'bin')):
        fp = os.path.join(dirs['installed'], 'bin', f)
        if f not in ['mpv.exe', 'mpv.com'] and (os.path.isfile(fp) or os.path.islink(fp)): os.remove(fp)

    try:
        logi('\n--- DLL DEPENDENCY CHECK ---')
        exe = os.path.join(dirs["installed"], "bin", "mpv.exe")
        out = subprocess.run([os.path.join(msys, 'ucrt64', 'bin', 'objdump.exe'), '-p', exe], capture_output=True, text=True).stdout
        dlls = [line.strip().split("DLL Name: ")[1] for line in out.splitlines() if "DLL Name:" in line]
        logi(f"Found DLLs: {', '.join(dlls)}")
        bad_dlls = [d for d in dlls if 'libstdc++' in d or 'zlib' in d or 'libgcc' in d or 'libEGL' in d]
        if bad_dlls:
             log.error(f'FAILURE: mpv.exe depends on: {bad_dlls}')
        else:
             logi('SUCCESS: mpv.exe has no forbidden DLL dependencies.')
        logi('----------------------------\n')
    except Exception as e: log.error(f'DLL Check Failed: {e}')
else: logi('Skipping mpv')

logi(f'Complete: {os.path.join(dirs["installed"], "bin", "mpv.exe")}')