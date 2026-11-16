# Analyze a public GitHub Issue

[Flask #2751](https://github.com/pallets/flask/issues/2751) is an example input for Issue retrieval and static repository analysis. These commands do not execute Flask or establish that its historical bug has been reproduced.

```bash
repro issue https://github.com/pallets/flask/issues/2751 --output issue.json
repro analyze https://github.com/pallets/flask/issues/2751 --output analysis.json
```

Both commands require GitHub access. Analysis reads the default branch unless you select a branch, tag, or full commit with `--ref`. The current default branch can have different code and dependencies from the historical Issue. Reporter statements and static repository facts remain separate in the output.

## Offline input

Save a GitHub REST Issue object containing `html_url`, `number`, `title`, `body`, and optionally `labels`. The included [synthetic payload](issue.json) demonstrates the format.

```text
repro issue --issue-json issue.json --output issue-context.json
repro analyze --issue-json issue.json --repository-path /path/to/local/repository --output analysis.json
```

The second command reads committed Git objects, not uncommitted working files. A local `origin` setting alone does not authenticate the repository's remote identity.

`GITHUB_TOKEN` can raise the API quota. It is used in requests to `api.github.com`; the acquisition environment does not forward it to Git or the sandbox. API, network, rate-limit, and unavailable-repository failures are reported explicitly. Local Issue JSON and a local repository allow analysis without network access.
