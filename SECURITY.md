# Security policy

## Supported versions

Only the latest release receives fixes.

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private reporting instead:
<https://github.com/projectbin07-stack/embedded-power-profiler/security/advisories/new>.

Include a description, a minimal input that triggers the problem and the version you used. Reports
are handled on a best-effort basis by a single maintainer.

## Scope

`epp` reads untrusted log files (text, CSV and the binary `.sslog` format). It never executes file
content, makes no network requests and writes only to the paths given on the command line
(`--report`, `--events-csv`, `--solar-node-sizer`, `synth -o`, `--states-out`). Reports of
interest include crashes or excessive memory/CPU use on crafted input and HTML injection through
file names or state names into the generated report.
