#!/usr/bin/env bash
# 直接调用 Kdenlive(flatpak) 自带的 melt / ffmpeg。
#
# 为什么要这个包装器
# ------------------
# 1. `flatpak run org.kde.kdenlive` 在本沙箱里会死在
#    `xdg-dbus-proxy` 握手（"与 dbus 代理同步失败"），所以避开 flatpak 包装器，
#    直接用 runtime 的 ld.so 加载 app 里的 melt。
# 2. 不能直接 `export LD_LIBRARY_PATH=...` —— flatpak runtime 里的 libm.so.6 需要
#    GLIBC_2.43，宿主的 head/timeout/bash 加载它会崩（"stack smashing detected"）。
#    必须用 `env VAR=... 命令` 只给目标进程，且 `timeout` 要放在 env 外层。
# 3. `~/Applications/kdenlive.AppImage` 是 0 字节空文件，不可用。
#
# 用法：
#   ./kdenlive_melt.sh -version
#   ./kdenlive_melt.sh <输入> -consumer avformat:out.mp4 ...
#   ./kdenlive_melt.sh --ffmpeg -i in.mp4 ...     # 用 flatpak 里的 ffmpeg
set -euo pipefail

APP=/var/lib/flatpak/app/org.kde.kdenlive/current/active/files
RT=/var/lib/flatpak/runtime/org.kde.Platform/x86_64/6.11/active/files

[ -d "$APP" ] || { echo "找不到 Kdenlive flatpak：$APP" >&2; exit 1; }
[ -d "$RT" ]  || { echo "找不到 KDE runtime：$RT" >&2; exit 1; }

LIBPATH="$APP/lib:$APP/lib/x86_64-linux-gnu:$RT/lib:$RT/lib/x86_64-linux-gnu"
LOADER="$RT/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2"

export_env=(
  "LD_LIBRARY_PATH=$LIBPATH"
  "MLT_REPOSITORY=$APP/lib/mlt-7"
  "MLT_DATA=$APP/share/mlt-7"
  "MLT_PROFILES=$APP/share/mlt-7/profiles"
  "MLT_SOFTWARE_SCALER=1"
)

if [ "${1:-}" = "--ffmpeg" ]; then
  shift
  exec env "${export_env[@]}" "$LOADER" --library-path "$LIBPATH" "$APP/bin/ffmpeg" "$@"
fi

exec env "${export_env[@]}" "$LOADER" --library-path "$LIBPATH" "$APP/bin/melt" "$@"
