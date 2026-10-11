# Published firmware

`firmware.bin` in this folder is what every panel updates to over the air.

Panels only update when the build number inside this file (`FW_BUILD` in the
sketch) is **higher** than the build they are running, so publishing the same
build twice does nothing.

Publish a new build with `tools/publish_firmware.ps1` (Windows) or
`tools/publish_firmware.sh` (WSL / Git Bash) — see the main README.

To stop a rollout, delete or revert `firmware.bin` and run `deploy/update.sh`
on the Pi. Panels that already updated stay on the new build.
