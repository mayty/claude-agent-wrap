<!-- This file has been edited with the assistance of an AI tool. -->
# Clipboard & WSLg Support

On WSL2 hosts with WSLg, `agent run` automatically exposes the host clipboard inside the container so Claude Code's `Ctrl+V` paste of Windows-clipboard images works out of the box. No configuration is needed.

This is a no-op on macOS and native Linux hosts.

## What the wrapper does

When `/mnt/wslg` exists on the host, `agent run`:

- bind-mounts `/mnt/wslg/.X11-unix` (at `/tmp/.X11-unix`) and `/mnt/wslg/runtime-dir` (the Wayland socket),
- forwards `DISPLAY` and `WAYLAND_DISPLAY` from the host shell and sets `XDG_RUNTIME_DIR=/mnt/wslg/runtime-dir`, and
- bind-mounts `wl-paste-shim` read-only over `/usr/local/bin/wl-paste` so it shadows the real binary via PATH order.

The wrapper deliberately mounts only those two sub-paths, **not** the whole `/mnt/wslg` tree: on WSL2 `/mnt/wslg/distro` is the host distro's entire root filesystem, and exposing it inside the container would loosen the sandbox's isolation.

## Why the shim is needed

WSLg advertises Windows-clipboard images as `image/bmp` only, but Claude Code's paste handler asks for `image/png`. The shim intercepts two cases:

- `--list-types` (or `-l`) — advertises `image/png` when only BMP is on the clipboard
- `--type image/png` (or `--type=image/png` / `-t image/png`) — fetches BMP and pipes it through ImageMagick's `convert bmp:- png:-`

Everything else falls through to the real `wl-paste`.

## Windows Terminal users

Windows Terminal intercepts `Ctrl+V` by default, so the keypress never reaches the container. Change the paste shortcut in Windows Terminal settings (Actions → Paste → set to something like `ctrl+shift+v`) for clipboard passthrough to work.

## Pasting images

Pasting an image copied from file explorer won't work — that copies a file path, not image data. Open the image in any viewer (Photos, Paint, browser) and copy it from there so the actual image data lands on the clipboard.
