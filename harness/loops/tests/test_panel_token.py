"""Token resolution precedence for the SonarQube reviewer."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import panel  # noqa: E402

SONAR = {"token_env": "SONARQUBE_TOKEN", "project_key": "acme_thing"}


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    monkeypatch.delenv("SONARQUBE_TOKEN", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))


def test_env_var_wins_over_dotenv(tmp_path, monkeypatch):
    """A stale .env must never silently shadow a deliberate export — that
    surfaces as an unexplained 401, not an obvious misconfiguration."""
    (tmp_path / ".env").write_text("SONARQUBE_TOKEN=from-dotenv\n")
    monkeypatch.setenv("SONARQUBE_TOKEN", "from-env")
    assert panel.resolve_token(SONAR, str(tmp_path)) == "from-env"


def test_dotenv_used_when_no_env_var(tmp_path):
    """The work-machine path: no export, no 1Password — the repo's .env is it."""
    (tmp_path / ".env").write_text("SONARQUBE_TOKEN=from-dotenv\n")
    assert panel.resolve_token(SONAR, str(tmp_path)) == "from-dotenv"


def test_dotenv_beats_the_cache(tmp_path):
    cache = tmp_path / "home" / ".cache" / "loops"
    cache.mkdir(parents=True)
    (cache / "sonar-acme_thing.token").write_text("from-cache\n")
    (tmp_path / ".env").write_text("SONARQUBE_TOKEN=from-dotenv\n")
    assert panel.resolve_token(SONAR, str(tmp_path)) == "from-dotenv"


def test_cache_used_when_no_env_and_no_dotenv(tmp_path):
    cache = tmp_path / "home" / ".cache" / "loops"
    cache.mkdir(parents=True)
    (cache / "sonar-acme_thing.token").write_text("from-cache\n")
    assert panel.resolve_token(SONAR, str(tmp_path)) == "from-cache"


def test_blank_dotenv_entry_falls_through(tmp_path):
    cache = tmp_path / "home" / ".cache" / "loops"
    cache.mkdir(parents=True)
    (cache / "sonar-acme_thing.token").write_text("from-cache\n")
    (tmp_path / ".env").write_text("SONARQUBE_TOKEN=\n")
    assert panel.resolve_token(SONAR, str(tmp_path)) == "from-cache"


def test_no_source_yields_empty(tmp_path):
    assert panel.resolve_token(SONAR, str(tmp_path)) == ""


def test_no_repo_path_still_reads_env(monkeypatch):
    monkeypatch.setenv("SONARQUBE_TOKEN", "from-env")
    assert panel.resolve_token(SONAR) == "from-env"


def test_tracked_dotenv_warns_but_still_resolves(repo_with_tracked_env, capsys):
    tok = panel.resolve_token(SONAR, str(repo_with_tracked_env))
    assert tok == "from-dotenv"
    assert "COMMITTED to git" in capsys.readouterr().err


@pytest.fixture
def repo_with_tracked_env(tmp_path):
    import subprocess
    r = tmp_path / "repo"
    r.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@e.com"],
                 ["config", "user.name", "T"]):
        subprocess.run(["git", "-C", str(r), *args], check=True)
    (r / ".env").write_text("SONARQUBE_TOKEN=from-dotenv\n")
    subprocess.run(["git", "-C", str(r), "add", "-f", ".env"], check=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "x"], check=True)
    return r
