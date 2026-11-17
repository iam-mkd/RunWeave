# Browser seccomp profile provenance

Derived from Microsoft Playwright v1.64.1:
https://github.com/microsoft/playwright/blob/v1.64.1/utils/docker/seccomp_profile.json

Upstream raw-file SHA-256:
`cc3e61cabda6bbc1e53e54d27ba4d55a9d3be829b6dd1a596f4a7b31b1cc7849`.
Upstream license: Apache 2.0; see PLAYWRIGHT-LICENSE alongside this file.

RunWeave modification: add an unconditional SCMP_ACT_ALLOW rule for `chroot`.
Chromium needs this inside its own user namespace when the outer container drops
all capabilities. No CAP_SYS_CHROOT is granted. Startup probes verify that outer
chroot remains denied, and Chromium must launch with its sandbox enabled.

Canonical JSON (sort_keys=True, separators=(',', ':')) SHA-256 after modification:
`77cdaf463cf5a605278debc2cfe13d5b09c1d5331fc3d4599b8f9466741c85e4`.
