"""Broad handlers returning a falsy value, so every caller reads "nothing found"
where the truth was "the lookup raised"."""


def head_sha(api, repo):
    try:
        return api.head(repo)
    except Exception:
        return None


def changed_files(api, pr):
    try:
        return api.files(pr)
    except (OSError, Exception):
        return []
