# Source review and remediation

Review baseline: `33a844751a9699cf364e9671b627158f0387424e`.
Review method: independent read-only inspection of the native adapter, pinned
Core bridge, host transport and ownership paths, plus focused Node VM
reproductions. No Unreal installation, UHT/UBT compile or native CEF execution
was involved. Source review is not a support certification.

## Findings addressed in the next source revision

1. **P2, startup ready lost to backpressure.** Core replays stub calls before
   emitting its one ready event. A full 256-entry business queue could drop the
   ready event and trigger a false native startup timeout. Loaded/load-error
   controls also competed for that capacity.
   - Change: separately acknowledged native `MarkReady`, plus coalesced reserved
     loaded/error/ready slots in the production mailbox. Errors drain first.
   - Regression: 256 startup calls still deliver ready; failed ready acknowledgement
     fails Core promises explicitly; a full native mailbox retains all controls;
     close/stop clears the control slots and rejects stale generations.

2. **P2, early invoke became a normal call.** The pinned upstream stub records
   `invoke` but Core's replay converts it to `call`, possibly executing a handler
   that the UE host intended to reject.
   - Change: a narrow startup compatibility shim rejects early `invoke` before
     it can enter Core's replay queue. Post-ready invoke retains its distinct
     envelope and receives the existing native unsupported error.
   - Regression: no host call is sent for early invoke; late invoke stays distinct.

3. **P2, early whenReady never settled.** The pinned stub does not hand its
   waiter to the installed bridge.
   - Change: the startup shim resolves through Core's existing ready event and
     rejects the waiter on pre-ready unload. Upstream assets remain unchanged.
   - Regression: early ready waiter resolves to the installed Core object;
     pre-ready unload rejects it.

4. **P3, malformed type with usable ID silently timed out.** Type validation
   returned before looking up a request ID.
   - Change: extract a usable ID first and return `INVALID_REQUEST` for a
     missing, empty or non-string type.
   - Regression: source ordering guard and a new real-Editor smoke branch.
     The native branch remains **not_run** until UHT/UBT and graphical execution.

The two upstream-stub defects have been reported for shared Core coordination.
The shim should be removed once a reviewed shared fix is pinned. No alternative
RPC implementation or modified copy of the upstream bridge was introduced.

## Additional source hardening

Each document now receives a unique subdomain under `.auroraview.invalid`,
separate from its endpoint token. This reduces accidental same-origin sharing;
it does not establish CEF storage/context isolation or hostile-script security.

## Risks deliberately left unverified

- Real `BindUObject` bool future shape, callback thread and ordering
- LoadString/about:blank/navigation completion and cancellation ordering
- Permanent binding visibility to blank/srcdoc/data frames and direct native calls
- UObject GC versus in-flight calls and nonblocking CEF teardown
- Browser self-close/renderer crash leaving stale ready state
- Module/host shutdown with pending calls and native resource leaks

The review did not establish a definite UAF, nor prove these paths safe.
Generation checks before dispatch and reply, session snapshots and copied
handlers address specific source-level reentrancy hazards, but the remaining
engine-dependent gates are retained in the acceptance matrix.

## Independent follow-up result

The follow-up source review of
`a1b1405beb7dfc40177d189d6f1f56eaacb5a925` confirmed all four findings were
addressed and found no new definite production defect. It independently reran
9 source checks, 4 preflight fixtures, 11 production-mailbox tests and 20 Node
tests. The 100-repeat and sanitizer evidence remains author-run evidence, not
independent reviewer execution.

After that review, production adapter code was frozen. Nonblocking test
suggestions added five Node regressions: actual delayed-transport startup order,
synchronous ready throw, missing ready binding, multiple early ready waiters,
and invoking a retained ready waiter after Core installation. The Unreal-only
smoke was expanded from missing request type to missing/empty/numeric variants;
it remains not_run. Documentation and test changes do not constitute UE build
or runtime verification.

The source candidate can be archived for review. Public release and any
Unreal support claim remain blocked on the real-engine acceptance gates.
