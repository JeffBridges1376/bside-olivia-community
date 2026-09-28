# Startup animation release input

The approved startup MP4 is a distributor-supplied release asset, not a tracked
source dependency. A clean clone and ordinary tests must work without it. Build
tools never discover ignored media from a developer's machine.

For a release that includes the animation, retrieve the approved MP4 from that
release's assets, verify its approved SHA-256, then pass both arguments:

```powershell
python -m installer build-update --source client --output update.oliviapatch --version VERSION --source-commit COMMIT --startup-video startup.mp4 --startup-video-sha256 APPROVED_SHA256
```

`installer/build_windows_setup.py` accepts the same two animation arguments in
addition to its usual setup arguments. The builders verify the copied bytes and
MP4 container header, stage `installer/assets/startup.mp4`, and write a
`startup.json` receipt containing the relative path, size, and hash. The update
package manifest also covers these files. Source paths are not included.

Omitting both arguments creates a package without the animation; this is valid
for source-only CI but does not satisfy an animation-bearing release. Providing
only one argument, an invalid file, or mismatching bytes fails the build. MP4
header/hash validation establishes identity, not visual or audio approval:
release acceptance must separately confirm the approved video and soundtrack.
