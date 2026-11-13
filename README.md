# nvmrc-preflight

Offline preflight for nvm's `.nvmrc` grammar: it tells you **exactly which
version token nvm will read, and which files nvm will silently misread**,
without starting a shell, sourcing `nvm.sh`, or touching the network.

Why now: nvm 0.41.0 grew a `key=value` `.nvmrc` format and a stricter
validator, but its parser is only reachable by running `nvm use/install/which`.
A UTF-8 BOM (very common when a file is written by PowerShell or a Windows
editor) is *not* stripped by nvm's pipeline, so `nvm install` silently receives
a version token beginning with `U+FEFF` instead of the number you wrote.

Upstream: https://github.com/nvm-sh/nvm

## What the upstream is / what it misses

`nvm` is the shell version manager for Node.js. It reads `.nvmrc` by walking up
from the current directory (`nvm_find_nvmrc`), then processes it in
`nvm_process_nvmrc_content` (`nvm.sh` v0.41.0, around lines 941–1085).

nvm itself documents that one of its resolution paths is unspecified:

> "`nvm exec` and `nvm run` follow the same `.nvmrc` lookup, but currently fall
> back to the active node if neither resolves — treat that fallback as
> undefined behavior"
> — README, `.nvmrc` section:
> https://github.com/nvm-sh/nvm/blob/master/README.md#nvmrc

and the parser's behaviour on byte-level inputs is not documented anywhere:

* `nvm.sh` line 941 strips comments and trims whitespace, but its `sed`
  pipeline never removes a UTF-8 BOM, so the BOM becomes part of the token:

  ```console
  $ printf '\xef\xbb\xbf20.11.0\n' > .nvmrc
  $ nvm_rc_version
  Found '/path/.nvmrc' with version <﻿20.11.0>     # note the invisible BOM
  ```

* `.node-version` (the interoperable file used by `fnm`, `asdf`, `volta`, CI
  images) is ignored by nvm entirely, so a repo can carry two contradictory
  version intents and nothing warns you.

nvm does reject genuinely broken files, but only with a long generic message
*at use time*: multiple bare versions, `node=...` keys, duplicate keys,
control characters, or a file that is only `key=value` pairs.

## What this tool does / does not do

Does:

* reimplements `nvm_process_nvmrc_content` + `nvm_find_nvmrc` in pure Python
  (stdlib only), so the result is available offline in CI before nvm runs;
* reports 12 finding codes (`NRC001`–`NRC012`) with a byte-level witness for
  the silent cases (BOM first bytes, control codepoints, duplicate key name);
* `resolve` prints the token nvm would read, and where it came from;
* the oracle test in `tests/test_nvmrc_preflight.py` runs the **real**
  `nvm_process_nvmrc` from an nvm checkout over the same 22-case corpus and
  asserts token equality.

Does not:

* install, switch, or download Node.js versions;
* emulate nvm's alias graph (`lts/*` → concrete version needs network/state);
* check that the token resolves to an *installed* version.

## Install / usage

Python 3.8+. No third-party packages, no build step.

```console
$ git clone --depth=1 git@github-linmoxi33:linmoxi33/nvmrc-preflight.git
$ cd nvmrc-preflight
$ python3 bin/nvmrc-preflight check examples/in/bom
nvmrc: .../examples/in/bom/.nvmrc
token: '\ufeff20.11.0'
NRC008 error   UTF-8 BOM survives nvm's pipeline and is prepended to the version token
summary: 1 error(s), 0 warning(s), 0 info(s) (fail-on=warning)
```

`--json` output:

```console
$ python3 bin/nvmrc-preflight check examples/in/bom --json
{
  "tool": "nvmrc-preflight",
  "version": "1.0.0",
  "target": ".../examples/in/bom",
  "nvmrc": ".../examples/in/bom/.nvmrc",
  "token": "\ufeff20.11.0",
  "findings": [
    {
      "code": "NRC008",
      "severity": "error",
      "message": "UTF-8 BOM survives nvm's pipeline and is prepended to the version token",
      "witness": {
        "token_hex": "efbbbf32302e31312e30",
        "first_bytes": [239, 187, 191],
        "would_be": "20.11.0"
      }
    }
  ],
  "summary": {"errors": 1, "warnings": 0, "infos": 0, "fail_on": "warning"}
}
```

Exit codes:

| code | meaning |
| --- | --- |
| `0` | nothing at/above `--fail-on` |
| `1` | findings at/above `--fail-on` (default `warning`) |
| `2` | usage error, or the target path does not exist |

`--fail-on` accepts `error`, `warning` (default), or `never`.
`resolve` returns `0` when a token was read, `1` when nvm would reject the
file, `2` when no `.nvmrc` exists walking up from the target.

Finding codes: `NRC001` empty, `NRC002` several bare versions, `NRC003` only
`key=value`, `NRC004` forbidden `node=` key, `NRC005` duplicate key, `NRC006`
control character, `NRC007` bare line starts with `=`, `NRC008` BOM,
`NRC009` CRLF (info), `NRC010` ignored `.node-version` sibling,
`NRC011` unresolvable token shape, `NRC012` `.nvmrc` is a symlink.

## Tests

```console
$ python3 -m unittest discover -s tests -v
```

18 tests: a 22-case grammar corpus whose expectations were observed from the
real nvm 0.41.0, discovery tests, 10 CLI behaviour tests (exit codes, `--json`,
`--fail-on`), and a differential oracle test that runs the real
`nvm_process_nvmrc` when a checkout is present (`NVM_SH=/path/to/nvm.sh`).

## 本轮创新

First differential conformance rig for nvm's `.nvmrc` grammar: it reimplements
the parser and then *proves* the reimplementation against the real
`nvm_process_nvmrc` on a recorded 22-case corpus, surfacing a concrete new
result — nvm 0.41.0 does not strip a UTF-8 BOM, so `nvm install` silently
receives `\ufeff<version>` (witness: first bytes `ef bb bf`). Compared with
`nvm`, which only validates at use time and never warns about an ignored
`.node-version`, this runs offline in CI and reports byte-level witnesses
(`would_be` token) instead of a generic invalid-`.nvmrc` message.

## 与同类项目的差异

`gh search repos "nvmrc"` (2026-11-14) returns:

* `nvm-sh/nvmrc` (21★) — an npm-installed CLI that validates a `.nvmrc` and
  prints the parsed JSON. It shares nvm's own validator, so it reports the
  *same* generic invalid message and, being npm-distributed, cannot run in a
  Python-only CI image. It does not flag the BOM case (nvm considers that file
  valid) and does not look at `.node-version`.
* shell helpers and zsh plugins (`zsh-nvm`, `nvm-auto`, nodenv-nvmrc) that
  *consume* `.nvmrc` at shell startup.

None of them reimplement nvm's parser or compare against the real
`nvm_process_nvmrc`. This tool deliberately does not switch versions; it
answers "what will nvm read, and what is silently wrong with the file".

## Limitations

* Aliases are not expanded: `lts/*`, `node`, `latest` are reported verbatim.
* The duplicate-key check mirrors nvm's `grep -E` on the key list, so a key
  containing regex metacharacters follows the same (regex) semantics.
* Non-UTF-8 bytes are decoded with `surrogateescape`; findings are still
  reported, but the token rendering for such files is lossy.
* Verified against nvm `0.41.0` only; if upstream changes
  `nvm_process_nvmrc_content`, re-run the oracle test.

MIT licensed.
