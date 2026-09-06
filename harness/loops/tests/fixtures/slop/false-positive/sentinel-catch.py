"""A broad handler that re-raises with context, and a narrow one whose sentinel
is an answer rather than a swallowed failure."""


def head_sha(api, repo):
    try:
        return api.head(repo)
    except Exception as exc:
        raise LookupError(f"no head commit for {repo}") from exc


def port_or_none(raw):
    try:
        return int(raw)
    except ValueError:
        return None
