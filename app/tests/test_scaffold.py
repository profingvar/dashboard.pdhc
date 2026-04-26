import os

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_required_files_exist():
    for f in ["readme.md", "progress.md", "changed_files.md", "newtask.txt",
              "top_rules.md", "requirements.txt", ".env.example", ".gitignore"]:
        assert os.path.exists(os.path.join(ROOT, f)), f"missing {f}"


def test_required_dirs_exist():
    for d in ["app", "app/routes", "app/services", "app/models", "app/templates",
              "app/static", "app/tests", "app/migrations", "docs", "results"]:
        assert os.path.isdir(os.path.join(ROOT, d)), f"missing dir {d}"
