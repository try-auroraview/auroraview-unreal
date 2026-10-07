# ADR 0001: Runtime host with an Editor adapter

Status: adopted for the experimental Win64 source candidate.

## Context

Both Editor and packaged games need AuroraView Core views and bidirectional external Python tools. UE4.18 has no embedded Editor Python plugin; the Unreal Python plugin is not a packaged-game runtime. The existing Editor-only browser implementation duplicated the boundary that other DCC adapters need to share.

## Decision

Move browser/session ownership, native GameThread dispatch, loopback communication and reflected/project tools into AuroraViewRuntime. Keep docking, selection, asset tooling, fixtures and Editor lifecycle policy in AuroraViewEditor. Preserve the Editor facade for existing C++ callers.

Reuse pinned Core call/invoke/result/event envelopes and upstream parent IPC v1. Add an explicitly negotiated RPC extension carried in named events. External Python remains a stdlib-only process, while optional Editor Python is discovered reflectively without linking its ABI. Use loaded-object reflection plus explicit project tools for native control.

Build every admitted engine with its own UHT/UBT and retain identity/integrity receipts. Treat Editor rendering, actual cooked Game execution and rendered Game CEF as separate gates. Generate a reproducible Chrome 59 bridge from pinned Core sources for UE4.

## Alternatives and consequences

Embedding a fixed Python ABI in every Unreal binary would increase legacy/compiler and packaged-game dependencies. Keeping everything in Editor would exclude the required Game scope. Introducing another RPC envelope would duplicate AuroraView semantics and make DCC interoperability harder. Attaching to arbitrary unmodified games is outside the plugin boundary.

The common Runtime API can control loaded reflected objects and project tools; it cannot make Editor-only or latent APIs synchronous Runtime functions. Native handlers run on GameThread and must remain bounded. External callbacks use bounded workers and cooperative completion. Authentication is explicit and loopback-only; broad control additionally requires an opt-in flag.

Upstream factory wiring and reusable bridge/transport conformance are follow-up work documented in [upstream integration](../upstream-integration.md). Unreal-specific handles, thread affinity and packaging remain in this adapter.
