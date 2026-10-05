#!/usr/bin/env bash
# Posts the tickets to the vendor repository. Run it yourself after `gh auth login`.
# One issue per file; stops at the first failure. Comment out the lines already posted.
set -euo pipefail
cd "$(dirname "$0")"
REPO=Acoustic-Control-Systems/a1580_examples

gh issue create --repo "$REPO" --title 'Pulser is enabled after power-on (TRAN:ENAB? returns 1), documentation says default OFF' --body-file 01-pulser-enabled-after-power-on.md
gh issue create --repo "$REPO" --title 'Power-on DATA:LENG is 114688: rejected by its own setter (-224) and the stream at that length is partly invalid' --body-file 02-power-on-data-length-out-of-range.md
gh issue create --repo "$REPO" --title '*RST does not reset any setting' --body-file 03-rst-resets-nothing.md
gh issue create --repo "$REPO" --title 'Opening a second TCP connection to port 5025 silently closes the first one' --body-file 04-second-scpi-connection-closes-first.md
gh issue create --repo "$REPO" --title 'Input buffer overrun: commands sent in a burst are discarded and a query in the burst is never answered' --body-file 05-input-buffer-overrun-drops-commands.md
gh issue create --repo "$REPO" --title 'MODE? and TRIG:MODE? return the syntax template (MASTer, INTernal) instead of a value' --body-file 06-enum-queries-return-syntax-template.md
gh issue create --repo "$REPO" --title 'Documentation: reply formats differ from SCPI_COMMANDS.md (booleans, time values, error text, DATA:PORT?)' --body-file 07-doc-reply-formats.md
gh issue create --repo "$REPO" --title 'Documentation: A-scan packet header fields (length, ctp, ascan_count) and behaviour of STOP / the data socket' --body-file 08-doc-packet-header-and-stop.md
gh issue create --repo "$REPO" --title 'Documentation: AVER:COUN semantics, automatic averaging delay, TRIG:DEL / time zero, error queue depth' --body-file 09-doc-averaging-trigger-delay-error-queue.md
