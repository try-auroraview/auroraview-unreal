# Source provenance

This candidate starts from the verified source archive containing the reviewed
implementation, not a reconstructed approximation of the native plugin.

- Original source commit recorded in the ZIP comment:
  `48753a8ac067529591a5fdba28a2c41c83850ea4`
- Exact restored Git tree: `d16b963b4c92119e9aa169986f05ccaf060b5f7b`
- Archive size: 48,766 bytes
- Archive SHA-256:
  `bdc3d3cbee15e96439647ea480c5f386f8fd39b1b5421707eee20f62fca09684`
- Restored archive CRC and source tree verified on 2026-10-06

The archive retains source files and its source-commit identifier; it does not
contain the original `.git` history. The local recovery commit is therefore a
new commit with an identical original source tree. Do not confuse its commit ID
with the historical source commit.

The subsequent experimental 5.7 adjustment changes only build admission,
preflight/tests, documentation and regenerated check evidence. It does not
modify the native C++ host implementation, endpoint, lifecycle mailbox, Core
assets or JavaScript transport. Upstream Core assets retain their original
commit/hash manifest and MIT notices.

Source review and cloud contract tests do not establish Unreal support.
UHT/UBT, actual CEF binding, graphical Editor use, GC and shutdown remain
`not_run` until separately validated on the chosen engine.
