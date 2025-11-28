# mpv-winbuild-python
Python script that builds mpv.exe at the push of a button

Pulls and installs everything, including MSYS2, in a subfolder. Builds and updates git-master of most projects. Rebuilds some dependencies only on change, others always for compatibility reasons. If dependency servers are accessible, you should end up with new/updated mpv.exe without further action. Works without admin privileges.

Builds mpv.exe with GCC and most features. Missing feature support: External storage media, binaural audio and ffmpeg CUDA filters (but NVDEC supported). Configure and linking take quite long with MSYS2 + GCC.

Optional command line arguments: --clean --force-resume --skip-updates --force-mpv

Configuring all projects to work correctly with static linking etc. was quite painful, this script documents all of these parameters and necessities in one file.

Might be AI generated.
