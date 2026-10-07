# Native evidence v2: fail-closed run verification

The verifier checks consistency and binding to separately trusted inputs. It does
not authenticate an observer, certify media contents, or replace independent
review of complete native logs, actual Editor state and recordings.

## Workflow

1. Freeze and independently verify the source delivery. Keep output evidence
   outside the source tree. Generate a pending run record with
   `python scripts/native_evidence.py --seed <evidence-directory>/record.json`.
2. A native execution owner records a clean exact source commit/tree/Core identity,
   the exact UE 5.7 Win64 engine version/BuildId, compiler name/version/command,
   build target and result. Archive the DLL, build log and native Automation JSON
   under the evidence directory. Retain failures and retries, not only successes.
3. An independent reviewer prepares a trusted run manifest and verifies its SHA-256
   out of band. Do not construct this trust anchor by blindly copying an unreviewed
   run record. Its fields are `run_id`, `source`, `engine`, `build`, `source_files`
   and, for extracted archives, `delivery_receipt`. The first four equal the
   independently accepted run identity. `source_files` maps every delivered source
   file's relative POSIX path to its SHA-256. Only `.git`, `__pycache__` and `.pyc`
   are excluded; additional source files cause a mismatch.
4. Fill actual measurements for each canonical criterion. Use one assertion per
   criterion with the same name, actual value, expected value, `operator: equal`
   and `observation: ue_automation` or `editor_state`. Current criteria are exact
   categorical/integer values. A run cannot introduce a tolerance; a future numeric
   criterion requires an explicitly reviewed canonical tolerance policy first.
5. Validate using `python scripts/native_evidence.py --validate record.json
   --trusted-run reviewed-run.json --trusted-run-sha256 <independently-verified-digest>
   --require-complete`. A valid pending seed is not complete native acceptance.

The runtime executes `acceptance/evidence.schema.json` with a small standard-library
schema interpreter. Unsupported schema keywords fail closed. Registry equality,
artifact/source verification, Automation results and clock checks are additional
mandatory semantic checks in the same validator. Validating the JSON Schema alone
is insufficient. No package install or network access is needed.

## Artifact and identity fields

Every artifact descriptor has `file` (relative to the evidence directory) and
`sha256`. Absolute paths, traversal, escapes through symlinks, missing files and
hash mismatches are rejected. Media also has `kind`, `host_time_s` and, for videos,
`video_time_s`.

The run's `build` contains `status`, `dll`, `log`, `receipt`, and
`automation_report`. A pass requires all four actual hashed files. The DLL must
have AMD64 PE32+ DLL headers. This is a format/architecture check, not signature
or provenance authentication.

The JSON build receipt contains `run_id`, `source`, `engine`, `dll`, `log`,
integer `exit_code: 0`, `target: UnrealEditor Win64 Development`, and a `compiler`
object with nonempty `name`, `version`, and `command`. It records what actually
ran. The trusted manifest separately pins the receipt and other artifact hashes.
Keep the original complete compiler output; the receipt does not replace it.

The strict Automation parser requires a `tests` array whose entries have
`fullTestPath` and `state`, plus integer `failed: 0`. Every canonical Automation
identity for a passed case must appear once with `state: Success`. This accepted
export shape still needs confirmation against the installed UE 5.7 run. If that
export differs, keep validation blocked and review an adapter against actual
installed output. Never rename an unrelated result or infer pass from process exit.

A local own-root Git checkout must also match its actual clean HEAD/tree. For an
extracted archive the trusted manifest must pin a delivery receipt artifact. Its
JSON contains `source`, `source_files`, and an `archive` artifact descriptor. The
archive must consist only of regular files/directories with exact root-relative
source paths, no prefix, no duplicates and no traversal. Its extracted file hashes,
the receipt file manifest, the trusted manifest and current source bytes must all
agree. An enclosing unrelated Git repository cannot supply archive identity.

## Policy and time

`required`, `implementation`, stage, steps, expected state and Automation identity
are fixed by the canonical case registry. Completion uses registry requirements,
not editable run-record flags. Missing/duplicate/unknown cases fail. Canonically
unimplemented cases remain visibly unimplemented. Every expected criterion needs
both a strictly typed matching actual value and its matching native assertion;
Python's bool/integer equality cannot discharge a criterion.

All pass times are timezone-aware UTC. `created_utc` is at or before recording
start, and each case starts at or after recording start and finishes at or after
its own start. The recording header must contain `started_utc` (video time zero)
and finite nonnegative `host_seconds_at_video_zero`. Case `host_time_s` and media
host times must fall inside that case's UTC interval when mapped through the
recording origin, allowing 0.25 seconds for measured clock alignment. Each video
point must also satisfy `abs(origin + video_time_s - host_time_s) <= 0.25`.
Nonfinite/negative times and NaN/Infinity anywhere in JSON are rejected.

Measure and record that origin in the native run. Do not invent it after the fact
to make mismatched recordings pass. Multiple recordings with different clock
origins require separate run records until a reviewed multi-clock format exists.
